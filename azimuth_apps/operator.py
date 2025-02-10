import asyncio
import base64
import contextlib
import datetime as dt
import functools
import json
import logging
import pathlib
import sys
import tempfile
import typing as t

import httpx
import kopf
import yaml

import easykube
import easysemver
import kube_custom_resource
import pyhelm3

from . import models
from .config import settings
from .models import v1alpha1 as api


LOGGER = logging.getLogger(__name__)


# Create an easykube client from the environment
from pydantic.json import pydantic_encoder
ekconfig = easykube.Configuration.from_environment(json_encoder = pydantic_encoder)
ekclient = ekconfig.async_client(default_field_manager = settings.easykube_field_manager)


helm_client = pyhelm3.Client(
    default_timeout = settings.helm_client.default_timeout,
    executable = settings.helm_client.executable,
    history_max_revisions = settings.helm_client.history_max_revisions,
    insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
    unpack_directory = settings.helm_client.unpack_directory
)


# Create a registry of custom resources and populate it from the models module
registry = kube_custom_resource.CustomResourceRegistry(
    settings.api_group,
    settings.crd_categories
)
registry.discover_models(models)


@kopf.on.startup()
async def apply_settings(**kwargs):
    """
    Apply kopf settings.
    """
    kopf_settings = kwargs["settings"]
    kopf_settings.persistence.finalizer = f"{settings.api_group}/finalizer"
    kopf_settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix = settings.api_group
    )
    kopf_settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix = settings.api_group,
        key = "last-handled-configuration",
    )
    kopf_settings.watching.client_timeout = settings.watch_timeout
    # Apply the CRDs
    for crd in registry:
        try:
            await ekclient.apply_object(crd.kubernetes_resource(), force = True)
        except Exception:
            LOGGER.exception("error applying CRD %s.%s - exiting", crd.plural_name, crd.api_group)
            sys.exit(1)
    # Give Kubernetes a chance to create the APIs for the CRDs
    await asyncio.sleep(0.5)
    # Check to see if the APIs for the CRDs are up
    # If they are not, the kopf watches will not start properly so we exit and get restarted
    LOGGER.info("Waiting for CRDs to become available")
    for crd in registry:
        preferred_version = next(k for k, v in crd.versions.items() if v.storage)
        api_version = f"{crd.api_group}/{preferred_version}"
        try:
            _ = await ekclient.get(f"/apis/{api_version}/{crd.plural_name}")
        except Exception:
            LOGGER.exception(
                "api for %s.%s not available - exiting",
                crd.plural_name,
                crd.api_group
            )
            sys.exit(1)


@kopf.on.cleanup()
async def on_cleanup(**kwargs):
    """
    Runs on operator shutdown.
    """
    LOGGER.info("Closing Kubernetes and Keycloak clients")
    await ekclient.aclose()


def format_instance(instance):
    """
    Formats an instance for logging.
    """
    if instance.metadata.name == instance.metadata.namespace:
        return instance.metadata.name
    else:
        return f"{instance.metadata.namespace}/{instance.metadata.name}"


async def ekresource_for_model(model, subresource = None):
    """
    Returns an easykube resource for the given model.
    """
    api = ekclient.api(f"{settings.api_group}/{model._meta.version}")
    resource = model._meta.plural_name
    if subresource:
        resource = f"{resource}/{subresource}"
    return await api.resource(resource)


async def save_instance_status(instance):
    """
    Save the status of the given instance.
    """
    ekresource = await ekresource_for_model(type(instance), "status")
    data = await ekresource.replace(
        instance.metadata.name,
        {
            # Include the resource version for optimistic concurrency
            "metadata": { "resourceVersion": instance.metadata.resource_version },
            "status": instance.status.model_dump(exclude_defaults = True),
        },
        namespace = instance.metadata.namespace
    )
    # Store the new resource version
    instance.metadata.resource_version = data["metadata"]["resourceVersion"]


def model_handler(model, register_fn, **kwargs):
    """
    Decorator that registers a handler with kopf for the specified model.
    """
    api_version = f"{settings.api_group}/{model._meta.version}"
    def decorator(func):
        @functools.wraps(func)
        async def handler(**handler_kwargs):
            if "instance" not in handler_kwargs:
                handler_kwargs["instance"] = model.model_validate(handler_kwargs["body"])
            try:
                return await func(**handler_kwargs)
            except easykube.ApiError as exc:
                if exc.status_code == 409:
                    # When a handler fails with a 409, we want to retry quickly
                    raise kopf.TemporaryError(str(exc), delay = 5)
                else:
                    raise
        return register_fn(api_version, model._meta.plural_name, **kwargs)(handler)
    return decorator


@model_handler(api.AppTemplate, kopf.on.create)
@model_handler(api.AppTemplate, kopf.on.update, field = "spec")
@model_handler(api.AppTemplate, kopf.on.timer, interval = settings.timer_interval)
async def reconcile_app_template(instance, **kwargs):
    """
    Reconciles an app template periodically.
    """
    # If there was a successful sync within the sync frequency, we don't need to do anything
    now = dt.datetime.now(dt.timezone.utc)
    threshold = now - dt.timedelta(seconds = instance.spec.sync_frequency)
    if instance.status.last_sync and instance.status.last_sync > threshold:
        return
    # Fetch the repository index from the specified URL
    async with httpx.AsyncClient(base_url = instance.spec.chart.repo) as http:
        response = await http.get("index.yaml")
        response.raise_for_status()
    # Get the available versions for the chart that match our constraint, sorted
    # with the most recent first
    version_range = easysemver.Range(instance.spec.version_range)
    chart_versions = sorted(
        (
            v
            for v in yaml.safe_load(response.text)["entries"][instance.spec.chart.name]
            if easysemver.Version(v["version"]) in version_range
        ),
        key = lambda v: easysemver.Version(v["version"]),
        reverse = True
    )
    # Throw away any versions that we aren't keeping
    chart_versions = chart_versions[:instance.spec.keep_versions]
    if not chart_versions:
        raise kopf.PermanentError("no versions matching constraint")
    next_label = instance.status.label
    next_logo = instance.status.logo
    next_description = instance.status.description
    next_versions = []
    # For each version, we need to make sure we have a values schema and optionally a UI schema
    for chart_version in chart_versions:
        existing_version = next(
            (
                version
                for version in instance.status.versions
                if version.name == chart_version["version"]
            ),
            None
        )
        # If we already know about the version, just use it as-is
        if existing_version:
            next_versions.append(existing_version)
            continue
        # Use the label, logo and description from the first version that has them
        # The label goes in a custom annotation as there isn't really a field for it, falling back
        # to the chart name if it is not present
        next_label = (
            next_label or
            chart_version.get("annotations", {}).get("azimuth.stackhpc.com/label") or
            chart_version["name"]
        )
        next_logo = next_logo or chart_version.get("icon")
        next_description = next_description or chart_version.get("description")
        # Pull the chart to extract the values schema and UI schema, if present
        chart_context = helm_client.pull_chart(
            instance.spec.chart.name,
            repo = instance.spec.chart.repo,
            version = chart_version["version"]
        )
        async with chart_context as chart:
            chart_directory = pathlib.Path(chart.ref)
            values_schema_file = chart_directory / "values.schema.json"
            ui_schema_file = chart_directory / "azimuth-ui.schema.yaml"
            if values_schema_file.is_file():
                with values_schema_file.open() as fh:
                    values_schema = json.load(fh)
            else:
                values_schema = {}
            if ui_schema_file.is_file():
                with ui_schema_file.open() as fh:
                    ui_schema = yaml.safe_load(fh)
            else:
                ui_schema = {}
        next_versions.append(
            api.AppTemplateVersion(
                name = chart_version["version"],
                values_schema = values_schema,
                ui_schema = ui_schema
            )
        )
    instance.status.label = instance.spec.label or next_label
    instance.status.logo = instance.spec.logo or next_logo
    instance.status.description = instance.spec.description or next_description
    instance.status.versions = next_versions
    instance.status.last_sync = dt.datetime.now(dt.timezone.utc)
    await save_instance_status(instance)


@model_handler(api.App, kopf.on.create)
@model_handler(api.App, kopf.on.update, field = "spec")
@model_handler(api.App, kopf.on.resume)
async def reconcile_app(instance: api.App, **kwargs):
    """
    Reconciles an app periodically.
    """
    # Initialise the phase as early as possible
    if instance.status.phase == api.AppPhase.UNKNOWN:
        instance.status.phase = api.AppPhase.PENDING
        await save_instance_status(instance)
    # Find the app, bailing if it doesn't exist
    apptemplates = await ekresource_for_model(api.AppTemplate)
    try:
        template_data = await apptemplates.fetch(instance.spec.template.name)
    except easykube.ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError("template does not exist")
        else:
            raise
    else:
        template = api.AppTemplate.model_validate(template_data)
    # Check that the specified version exists
    if not any(v.name == instance.spec.template.version for v in template.status.versions):
        raise kopf.TemporaryError("requested template version does not exist")
    # Template out and apply the Flux resources for the app
    for resource in [
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "HelmRepository",
            "metadata": {
                "name": instance.metadata.name,
                "namespace": instance.metadata.namespace,
                "labels": {
                    "apps.azimuth-cloud.io/app": instance.metadata.name,
                },
            },
            "spec": {
                "url": template.spec.chart.repo,
                "interval": "1h",
            },
        },
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "HelmChart",
            "metadata": {
                "name": instance.metadata.name,
                "namespace": instance.metadata.namespace,
                "labels": {
                    "apps.azimuth-cloud.io/app": instance.metadata.name,
                },
            },
            "spec": {
                "chart": template.spec.chart.name,
                "version": instance.spec.template.version,
                "sourceRef": {
                    "kind": "HelmRepository",
                    "name": instance.metadata.name,
                },
                "interval": "1h",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{instance.metadata.name}-config",
                "namespace": instance.metadata.namespace,
                "labels": {
                    "apps.azimuth-cloud.io/app": instance.metadata.name,
                },
            },
            "stringData": {
                "values.yaml": yaml.safe_dump(instance.spec.values),
            },
        },
        {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {
                "name": instance.metadata.name,
                "namespace": instance.metadata.namespace,
                "labels": {
                    "apps.azimuth-cloud.io/app": instance.metadata.name,
                },
            },
            "spec": {
                "chartRef": {
                    "kind": "HelmChart",
                    "name": instance.metadata.name,
                },
                # Use the specified kubeconfig
                "kubeConfig": {
                    "secretRef": {
                        "name": instance.spec.kubeconfig_secret.name,
                        "key": instance.spec.kubeconfig_secret.key,
                    },
                },
                # Each app has a named namespace
                "targetNamespace": instance.metadata.name,
                # Store Helm release information in the same namespace
                "storageNamespace": instance.metadata.name,
                # The release is named after the app
                "releaseName": instance.metadata.name,
                # Values come from the secret that we wrote
                "valuesFrom": [
                    {
                        "kind": "Secret",
                        "name": f"{instance.metadata.name}-config",
                        "valuesKey": "values.yaml",
                    },
                ],
                # For maximum compatibility, don't use a persistent client
                # https://fluxcd.io/flux/components/helm/helmreleases/#persistent-client
                "persistentClient": False,
                "install": {
                    "crds": "CreateReplace",
                    "createNamespace": True,
                    "remediation": {
                        "retries": -1,
                    },
                },
                "upgrade": {
                    "crds": "CreateReplace",
                    "remediation": {
                        "retries": -1,
                    },
                },
                "interval": "5m",
                "timeout": "5m",
            },
        },
    ]:
        # Make sure the Flux resources are owned by the app resource
        kopf.adopt(resource, instance.model_dump(by_alias = True))
        await ekclient.apply_object(resource, force = True)


@model_handler(api.App, kopf.on.delete)
async def delete_app(instance, **kwargs):
    """
    Handles the deletion of an app.
    """
    # The Flux resources will be deleted by virtue of the owner references
    # The status will be updated by the timer tracking the Flux resources
    # We don't want our finalizer to be removed unless the Flux HelmRelease is gone
    releases = await ekclient.api("helm.toolkit.fluxcd.io/v2").resource("helmreleases")
    try:
        release = await releases.fetch(
            instance.metadata.name,
            namespace = instance.metadata.namespace
        )
    except easykube.ApiError as exc:
        # If the status is 404, we are done
        if exc.status_code != 404:
            raise
    else:
        # If the deletion of the HelmRelease has not been triggered yet, trigger it
        if not release.metadata.get("deletionTimestamp"):
            await ekclient.delete_object(release)
        raise kopf.TemporaryError("waiting for Flux HelmRelease to be deleted", delay = 5)


@contextlib.asynccontextmanager
async def clients_for_app(app: api.App):
    """
    Context manager that yields a tuple of (easykube client, Helm client) configured to
    target the cluster that the given app is installed on.
    """
    # Load the kubeconfig secret specified by the app
    secrets = await ekclient.api("v1").resource("secrets")
    try:
        kubeconfig_secret = await secrets.fetch(
            app.spec.kubeconfig_secret.name,
            namespace = app.metadata.namespace
        )
    except easykube.ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError("kubeconfig secret does not exist", delay = 15)
        else:
            raise
    try:
        kubeconfig_data_b64 = kubeconfig_secret.data[app.spec.kubeconfig_secret.key]
    except KeyError:
        raise kopf.TemporaryError("specified key does not exist in kubeconfig secret")
    kubeconfig_data = base64.b64decode(kubeconfig_data_b64)
    # We pass a Helm client that is configured for the target cluster
    with tempfile.NamedTemporaryFile() as kubeconfig:
        kubeconfig.write(kubeconfig_data)
        kubeconfig.flush()
        # Get an easykube client for the target cluster
        ekclient_target = (
            easykube.Configuration
                .from_kubeconfig_data(kubeconfig_data, json_encoder = pydantic_encoder)
                .async_client(default_field_manager = settings.easykube_field_manager)
        )
        # Get a Helm client for the target cluster
        helm_client_target = pyhelm3.Client(
            default_timeout = settings.helm_client.default_timeout,
            executable = settings.helm_client.executable,
            history_max_revisions = settings.helm_client.history_max_revisions,
            insecure_skip_tls_verify = settings.helm_client.insecure_skip_tls_verify,
            kubeconfig = kubeconfig.name,
            unpack_directory = settings.helm_client.unpack_directory
        )
        # Yield the clients as a tuple
        async with ekclient_target:
            yield (ekclient_target, helm_client_target)


async def get_helm_revision(helm_client, release) -> pyhelm3.ReleaseRevisionStatus:
    """
    Returns the current Helm revision for the specified HelmRelease object.
    """
    try:
        return await helm_client.get_current_revision(
            release["spec"]["releaseName"],
            namespace = release["spec"]["storageNamespace"]
        )
    except pyhelm3.errors.ReleaseNotFoundError:
        raise kopf.TemporaryError("helm release for app does not exist")


async def get_zenith_services(ekclient, revision):
    """
    Returns the current state of any Zenith services defined in the given Helm revision.
    """
    # Extract the name and namespace of any Zenith reservations in the manifest
    manifest_reservations = [
        (
            resource["metadata"]["name"],
            resource["metadata"].get("namespace", revision.release.namespace)
        )
        for resource in (await revision.resources())
        if (
            resource["apiVersion"] == "zenith.stackhpc.com/v1alpha1" and
            resource["kind"] == "Reservation"
        )
    ]
    # If there are no reservations in the manifest, we are done
    if not manifest_reservations:
        return {}
    # Otherwise, load the current state of each reservation from the target cluster
    services = {}
    reservations = await ekclient.api("zenith.stackhpc.com/v1alpha1").resource("reservations")
    for reservation_name, reservation_namespace in manifest_reservations:
        reservation = await reservations.fetch(reservation_name, namespace = reservation_namespace)
        # If the reservation is not ready, leave it for now
        if reservation.get("status", {}).get("phase", "Unknown") != "Ready":
            continue
        annotations = reservation["metadata"].get("annotations", {})
        # Derive the key to store the reservation status under
        reservation_key = (
            reservation_name
            if reservation_name.startswith(reservation_namespace)
            else f"{reservation_namespace}-{reservation_name}"
        )
        services[reservation_key] = api.AppServiceStatus(
            subdomain = reservation["status"]["subdomain"],
            fqdn = reservation["status"]["fqdn"],
            label = annotations.get(
                "azimuth.stackhpc.com/service-label",
                # Derive a label from the name if not specified
                " ".join(word.capitalize() for word in reservation_name.split("-"))
            ),
            icon_url = annotations.get("azimuth.stackhpc.com/service-icon-url"),
            description = annotations.get("azimuth.stackhpc.com/service-description")
        )
    return services


async def update_identity_platform(app: api.App):
    """
    Makes sure that the identity platform for the app matches the current state of the
    Zenith services for the app.
    """
    platform = {
        "apiVersion": "identity.azimuth.stackhpc.com/v1alpha1",
        "kind": "Platform",
        "metadata": {
            "name": f"kubeapp-{app.metadata.name}",
            "namespace": app.metadata.namespace,
        },
        "spec": {
            "realmName": app.spec.zenith_identity_realm_name,
            "zenithServices": {
                name: {
                    "subdomain": service.subdomain,
                    "fqdn": service.fqdn,
                }
                for name, service in app.status.services.items()
            },
        },
    }
    kopf.adopt(platform, app.model_dump(by_alias = True))
    await ekclient.apply_object(platform, force = True)


async def reconcile_app_status(app: api.App, release: t.Dict[str, t.Any]):
    """
    Reconcile the status of an app.
    """
    # NOTE(mkjpryor)
    #
    # Currently, the notes from the Helm release - which we use as the usage docs - are
    # not exposed as part of the HelmRelease resource
    #
    # We also need to get the name and namespace of any Zenith reservations that were
    # created as part of the release so that we can add them to our services
    #
    # This means that we need to load the Helm release from the target cluster
    #
    # Since we are already fetching the Helm release, we might as well get the status from
    # it as well, for now, as it maps nicely onto our current phases rather than trying
    # to derive it from the conditions of the HelmRelease object
    #
    # Flux may implement additional functionality in the future, e.g. custom health checks,
    # that changes this situation

    # If the app is deleting, we don't do anything
    if app.metadata.deletion_timestamp:
        return

    async with clients_for_app(app) as (ekclient_target, helm_client_target):
        # Get the current revision of the Helm release managed by the Flux HelmRelease
        revision = await get_helm_revision(helm_client_target, release)
        # Derive the status from the revision
        app.status.usage = revision.notes
        app.status.failure_message = None
        if revision.status == pyhelm3.ReleaseRevisionStatus.DEPLOYED:
            app.status.phase = api.AppPhase.DEPLOYED
        elif revision.status == pyhelm3.ReleaseRevisionStatus.FAILED:
            app.status.phase = api.AppPhase.FAILED
            app.status.failure_message = revision.description
        elif revision.status == pyhelm3.ReleaseRevisionStatus.PENDING_ROLLBACK:
            app.status.phase = api.AppPhase.PREPARING
        elif revision.status == pyhelm3.ReleaseRevisionStatus.PENDING_INSTALL:
            app.status.phase = api.AppPhase.INSTALLING
        elif revision.status == pyhelm3.ReleaseRevisionStatus.PENDING_UPGRADE:
            app.status.phase = api.AppPhase.UPGRADING
        elif revision.status in {
            pyhelm3.ReleaseRevisionStatus.UNINSTALLING,
            pyhelm3.ReleaseRevisionStatus.UNINSTALLED
        }:
            app.status.phase = api.AppPhase.UNINSTALLING
        else:
            # superseded or unknown
            app.status.phase = api.AppPhase.UNKNOWN
        # Update the Zenith services from the Helm revision
        app.status.services = await get_zenith_services(ekclient_target, revision)
        # Save the changes to the status
        await save_instance_status(app)
    # Make sure the identity platform that produces credentials for Zenith is up-to-date
    await update_identity_platform(app)


@model_handler(
    api.App, kopf.on.timer,
    interval = settings.timer_interval,
    idle = settings.timer_interval
)
async def reconcile_app_status_timer(instance: api.App, **kwargs):
    """
    Reconcile the app status.
    """
    # Get the HelmRelease associated with the app
    releases = await ekclient.api("helm.toolkit.fluxcd.io/v2").resource("helmreleases")
    release = await releases.first(
        labels = {"apps.azimuth-cloud.io/app": instance.metadata.name},
        namespace = instance.metadata.namespace
    )
    # If there is no release yet, we have nothing to do
    # If there is a release, reconcile the app status with it
    if release:
        await reconcile_app_status(instance, release)


@kopf.on.event(
    "helmreleases.helm.toolkit.fluxcd.io",
    labels = {"apps.azimuth-cloud.io/app": kopf.PRESENT}
)
async def handle_helmrelease_event(body, namespace, labels, **kwargs):
    """
    Handles changes to Flux HelmRelease objects associated with apps.
    """
    # Get the app associated with the HelmRelease
    apps = await ekresource_for_model(api.App)
    try:
        app_data = await apps.fetch(labels["apps.azimuth-cloud.io/app"], namespace = namespace)
    except easykube.ApiError as exc:
        if exc.status_code != 404:
            raise
    else:
        app = api.App.model_validate(app_data)
        await reconcile_app_status(app, body)
