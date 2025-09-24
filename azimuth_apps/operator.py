import asyncio
import base64
import contextlib
import dataclasses
import datetime as dt
import functools
import hashlib
import json
import logging
import pathlib
import random
import sys
import tempfile
import typing as t

import easykube
import easysemver
import httpx
import kopf
import kube_custom_resource
import pyhelm3
import yaml

# Create an easykube client from the environment
from pydantic.json import pydantic_encoder

from . import models
from .config import settings
from .models import v1alpha1 as api

LOGGER = logging.getLogger(__name__)


ekconfig = easykube.Configuration.from_environment(json_encoder=pydantic_encoder)
ekclient = ekconfig.async_client(default_field_manager=settings.easykube_field_manager)


helm_client = pyhelm3.Client(
    default_timeout=settings.helm_client.default_timeout,
    executable=settings.helm_client.executable,
    history_max_revisions=settings.helm_client.history_max_revisions,
    insecure_skip_tls_verify=settings.helm_client.insecure_skip_tls_verify,
    unpack_directory=settings.helm_client.unpack_directory,
)


# Create a registry of custom resources and populate it from the models module
registry = kube_custom_resource.CustomResourceRegistry(
    settings.api_group, settings.crd_categories
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
        prefix=settings.api_group
    )
    kopf_settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix=settings.api_group,
        key="last-handled-configuration",
    )
    kopf_settings.watching.client_timeout = settings.watch_timeout
    # Apply the CRDs
    for crd in registry:
        try:
            await ekclient.apply_object(crd.kubernetes_resource(), force=True)
        except Exception:
            LOGGER.exception(
                "error applying CRD %s.%s - exiting", crd.plural_name, crd.api_group
            )
            sys.exit(1)
    # Give Kubernetes a chance to create the APIs for the CRDs
    await asyncio.sleep(0.5)
    # Check to see if the APIs for the CRDs are up. If they are not,
    # the kopf watches will not start properly so we exit and get restarted
    LOGGER.info("Waiting for CRDs to become available")
    for crd in registry:
        preferred_version = next(k for k, v in crd.versions.items() if v.storage)
        api_version = f"{crd.api_group}/{preferred_version}"
        try:
            _ = await ekclient.get(f"/apis/{api_version}/{crd.plural_name}")
        except Exception:
            LOGGER.exception(
                "api for %s.%s not available - exiting", crd.plural_name, crd.api_group
            )
            sys.exit(1)


@kopf.on.cleanup()
async def on_cleanup(**kwargs):
    """
    Runs on operator shutdown.
    """
    LOGGER.info("Closing Kubernetes and Keycloak clients")
    await ekclient.aclose()


async def ekresource_for_model(model, subresource=None):
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
            "metadata": {"resourceVersion": instance.metadata.resource_version},
            "status": instance.status.model_dump(exclude_defaults=True),
        },
        namespace=instance.metadata.namespace,
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
                handler_kwargs["instance"] = model.model_validate(
                    handler_kwargs["body"]
                )
            try:
                return await func(**handler_kwargs)
            except easykube.ApiError as exc:
                if exc.status_code == 409:
                    # When a handler fails with a 409, we want to retry quickly
                    raise kopf.TemporaryError(str(exc), delay=5)
                else:
                    raise

        return register_fn(api_version, model._meta.plural_name, **kwargs)(handler)

    return decorator


@model_handler(api.AppTemplate, kopf.on.create)
@model_handler(api.AppTemplate, kopf.on.update, field="spec")
@model_handler(api.AppTemplate, kopf.on.timer, interval=settings.timer_interval)
async def reconcile_app_template(instance, **kwargs):
    """
    Reconciles an app template periodically.
    """
    # If there was a successful sync within the sync frequency, we don't need to do
    # anything
    now = dt.datetime.now(dt.timezone.utc)
    threshold = now - dt.timedelta(seconds=instance.spec.sync_frequency)
    if instance.status.last_sync and instance.status.last_sync > threshold:
        return
    # Fetch the repository index from the specified URL
    async with httpx.AsyncClient(base_url=instance.spec.chart.repo) as http:
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
        key=lambda v: easysemver.Version(v["version"]),
        reverse=True,
    )
    # Throw away any versions that we aren't keeping
    chart_versions = chart_versions[: instance.spec.keep_versions]
    if not chart_versions:
        raise kopf.PermanentError("no versions matching constraint")
    next_label = instance.status.label
    next_logo = instance.status.logo
    next_description = instance.status.description
    next_versions = []
    # For each version, we need to make sure we have a values schema and optionally a UI
    # schema
    for chart_version in chart_versions:
        existing_version = next(
            (
                version
                for version in instance.status.versions
                if version.name == chart_version["version"]
            ),
            None,
        )
        # If we already know about the version, just use it as-is
        if existing_version:
            next_versions.append(existing_version)
            continue
        # Use the label, logo and description from the first version that has them
        # The label goes in a custom annotation as there isn't really a field for it,
        # falling back to the chart name if it is not present
        next_label = (
            next_label
            or chart_version.get("annotations", {}).get("azimuth.stackhpc.com/label")
            or chart_version["name"]
        )
        next_logo = next_logo or chart_version.get("icon")
        next_description = next_description or chart_version.get("description")
        # Pull the chart to extract the values schema and UI schema, if present
        chart_context = helm_client.pull_chart(
            instance.spec.chart.name,
            repo=instance.spec.chart.repo,
            version=chart_version["version"],
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
                name=chart_version["version"],
                values_schema=values_schema,
                ui_schema=ui_schema,
            )
        )
    instance.status.label = instance.spec.label or next_label
    instance.status.logo = instance.spec.logo or next_logo
    instance.status.description = instance.spec.description or next_description
    instance.status.versions = next_versions
    instance.status.last_sync = dt.datetime.now(dt.timezone.utc)
    await save_instance_status(instance)


def compute_checksum(data):
    """
    Computes the checksum for the given data.
    """
    # We compute the checksum by dumping the data as JSON and hashing it
    # When dumping, we sort the keys to try and ensure consistency
    if not isinstance(data, str):
        data = json.dumps(data, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def generate_flux_resources(
    owner: dict[str, t.Any],
    name: str,
    namespace: str,
    labels: dict[str, str],
    repo: str,
    chart: str,
    version: str,
    values: dict[str, str],
    release_name: str | None = None,
    target_namespace: str | None = None,
    kubeconfig_secret_name: str | None = None,
    kubeconfig_secret_key: str | None = None,
    management_cluster_install=False,
):
    if management_cluster_install:
        values["targetNamespace"] = target_namespace
        values["kubeconfig"] = {
            "name": kubeconfig_secret_name,
            "key": kubeconfig_secret_key,
        }

    return [
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "HelmRepository",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "ownerReferences": [
                    {
                        "apiVersion": owner["apiVersion"],
                        "kind": owner["kind"],
                        "name": owner["metadata"]["name"],
                        "uid": owner["metadata"]["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    },
                ],
            },
            "spec": {
                "url": repo,
                "interval": "1h",
            },
        },
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "HelmChart",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    # We want the chart to reconcile whenever the chart repo changes
                    "reconcile.fluxcd.io/requestedAt": compute_checksum(repo),
                },
                "ownerReferences": [
                    {
                        "apiVersion": owner["apiVersion"],
                        "kind": owner["kind"],
                        "name": owner["metadata"]["name"],
                        "uid": owner["metadata"]["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    },
                ],
            },
            "spec": {
                "chart": chart,
                "version": version,
                "sourceRef": {
                    "kind": "HelmRepository",
                    "name": name,
                },
                "interval": "1h",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{name}-config",
                "namespace": namespace,
                "labels": labels,
                "ownerReferences": [
                    {
                        "apiVersion": owner["apiVersion"],
                        "kind": owner["kind"],
                        "name": owner["metadata"]["name"],
                        "uid": owner["metadata"]["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    },
                ],
            },
            "stringData": {
                "values.yaml": yaml.safe_dump(values),
            },
        },
        {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    # We want the Helm release to reconcile whenever the chart or values
                    # change
                    "reconcile.fluxcd.io/requestedAt": compute_checksum(
                        {
                            "chart": {
                                "repo": repo,
                                "name": chart,
                                "version": version,
                            },
                            "values": values,
                        }
                    ),
                },
                "ownerReferences": [
                    {
                        "apiVersion": owner["apiVersion"],
                        "kind": owner["kind"],
                        "name": owner["metadata"]["name"],
                        "uid": owner["metadata"]["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    },
                ],
            },
            "spec": {
                "chartRef": {
                    "kind": "HelmChart",
                    "name": name,
                },
                "releaseName": release_name or name,
                "targetNamespace": target_namespace or namespace,
                "storageNamespace": target_namespace or namespace,
                "valuesFrom": [
                    {
                        "kind": "Secret",
                        "name": f"{name}-config",
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
                # If a kubeconfig secret is specified, configure it
                **(
                    {
                        "kubeConfig": {
                            "secretRef": {
                                "name": kubeconfig_secret_name,
                                "key": kubeconfig_secret_key,
                            },
                        },
                    }
                    if kubeconfig_secret_name and not management_cluster_install
                    else {}
                ),
            },
        },
    ]


@kopf.on.event(
    "v1",
    "secrets",
    labels={settings.zenith_operator.kubeconfig_secret_label: kopf.PRESENT},
)
async def handle_secret_event(logger, name, namespace, body, **kwargs):
    """
    Handles events on kubeconfig secrets by ensuring that Flux resources exist
    to provision a Zenith operator watching the cluster.
    """
    # If the secret is deleting, we do nothing as the Flux resources will be deleted by
    # garbage collection
    if body["metadata"].get("deletionTimestamp"):
        return
    # We retry the event, with an exponential backoff, until it succeeds
    attempt = 0
    while True:
        try:
            # Template out and apply the Flux resources for the Zenith operator instance
            for resource in generate_flux_resources(
                body,
                f"{name}-zenith-operator",
                namespace,
                {"app.kubernetes.io/managed-by": "azimuth-apps-operator"},
                settings.zenith_operator.chart_repo,
                settings.zenith_operator.chart_name,
                settings.zenith_operator.chart_version,
                {
                    **settings.zenith_operator.default_values,
                    # Use the secret that the event is for as the target
                    "kubeconfigSecret": {
                        "name": name,
                        # Just use the first key in the secret
                        # We expect a single key
                        "key": next(iter(body["data"].keys())),
                    },
                },
            ):
                await ekclient.apply_object(resource, force=True)
        except Exception as exc:
            # Log the exception
            logger.exception(exc)
        else:
            return
        # Calculate the clamped backoff, including some jitter
        delay = min(2**attempt, 120) + random.uniform(0, 1)
        logger.info("Waiting %.3fs before retrying", delay)
        await asyncio.sleep(delay)
        attempt = attempt + 1


@model_handler(api.App, kopf.on.create)
@model_handler(api.App, kopf.on.update, field="spec")
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

    # Template out and apply the Flux resources for the app
    for resource in generate_flux_resources(
        instance.model_dump(by_alias=True),
        f"azapp-{instance.metadata.name}",
        instance.metadata.namespace,
        {
            "app.kubernetes.io/managed-by": "azimuth-apps-operator",
            "apps.azimuth-cloud.io/app": instance.metadata.name,
        },
        template.spec.chart.repo,
        template.spec.chart.name,
        instance.spec.template.version,
        instance.spec.values,
        instance.metadata.name,
        (
            instance.metadata.namespace
            if template.spec.management_install
            else template.spec.namespace or instance.metadata.name
        ),
        instance.spec.kubeconfig_secret.name,
        instance.spec.kubeconfig_secret.key,
        template.spec.management_install,
    ):
        await ekclient.apply_object(resource, force=True)


@model_handler(api.App, kopf.on.delete)
async def delete_app(instance, **kwargs):
    """
    Handles the deletion of an app.
    """
    # Put the app into the uninstalling phase if it isn't already
    if instance.status.phase != api.AppPhase.UNINSTALLING:
        instance.status.phase = api.AppPhase.UNINSTALLING
        await save_instance_status(instance)
    # The Flux resources will be deleted by virtue of the owner references
    # The status will be updated by the timer tracking the Flux resources
    # We don't want our finalizer to be removed unless the Flux HelmRelease is gone
    releases = await ekclient.api("helm.toolkit.fluxcd.io/v2").resource("helmreleases")
    try:
        release = await releases.fetch(
            instance.metadata.name, namespace=instance.metadata.namespace
        )
    except easykube.ApiError as exc:
        # If the status is 404, we are done
        if exc.status_code != 404:
            raise
    else:
        # If the deletion of the HelmRelease has not been triggered yet, trigger it
        if not release.metadata.get("deletionTimestamp"):
            await ekclient.delete_object(release)
        raise kopf.TemporaryError("waiting for Flux HelmRelease to be deleted", delay=5)


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
            app.spec.kubeconfig_secret.name, namespace=app.metadata.namespace
        )
    except easykube.ApiError as exc:
        if exc.status_code == 404:
            raise kopf.TemporaryError("kubeconfig secret does not exist", delay=15)
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
        ekclient_target = easykube.Configuration.from_kubeconfig_data(
            kubeconfig_data, json_encoder=pydantic_encoder
        ).async_client(default_field_manager=settings.easykube_field_manager)
        # Get a Helm client for the target cluster
        helm_client_target = pyhelm3.Client(
            default_timeout=settings.helm_client.default_timeout,
            executable=settings.helm_client.executable,
            history_max_revisions=settings.helm_client.history_max_revisions,
            insecure_skip_tls_verify=settings.helm_client.insecure_skip_tls_verify,
            kubeconfig=kubeconfig.name,
            unpack_directory=settings.helm_client.unpack_directory,
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
            namespace=release["spec"]["storageNamespace"],
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
            resource["metadata"].get("namespace", revision.release.namespace),
        )
        for resource in (await revision.resources())
        if (
            resource["apiVersion"] == "zenith.stackhpc.com/v1alpha1"
            and resource["kind"] == "Reservation"
        )
    ]
    # If there are no reservations in the manifest, we are done
    if not manifest_reservations:
        return {}
    # Otherwise, load the current state of each reservation from the target cluster
    services = {}
    reservations = await ekclient.api("zenith.stackhpc.com/v1alpha1").resource(
        "reservations"
    )
    for reservation_name, reservation_namespace in manifest_reservations:
        reservation = await reservations.fetch(
            reservation_name, namespace=reservation_namespace
        )
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
            subdomain=reservation["status"]["subdomain"],
            fqdn=reservation["status"]["fqdn"],
            label=annotations.get(
                "azimuth.stackhpc.com/service-label",
                # Derive a label from the name if not specified
                " ".join(word.capitalize() for word in reservation_name.split("-")),
            ),
            icon_url=annotations.get("azimuth.stackhpc.com/service-icon-url"),
            description=annotations.get("azimuth.stackhpc.com/service-description"),
        )
    return services


async def update_identity_platform(app: api.App):
    """
    Makes sure that the identity platform for the app matches the current state of the
    Zenith services for the app.
    """
    await ekclient.apply_object(
        {
            "apiVersion": "identity.azimuth.stackhpc.com/v1alpha1",
            "kind": "Platform",
            "metadata": {
                "name": f"azapp-{app.metadata.name}",
                "namespace": app.metadata.namespace,
                "ownerReferences": [
                    {
                        "apiVersion": app.api_version,
                        "kind": app.kind,
                        "name": app.metadata.name,
                        "uid": app.metadata.uid,
                        "controller": True,
                        "blockOwnerDeletion": True,
                    },
                ],
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
        },
        force=True,
    )


@dataclasses.dataclass
class Condition:
    status: str
    reason: str | None
    message: str | None

    def __str__(self):
        return self.message or self.reason or ""


def extract_condition(obj, type) -> Condition:  # noqa: A002
    """
    Extract the status and reason from the condition of the given type.
    """
    conditions = obj.get("status", {}).get("conditions", [])
    try:
        condition = next(c for c in conditions if c["type"] == type)
    except StopIteration:
        return Condition("Unknown", None, None)
    else:
        return Condition(
            condition["status"], condition.get("reason"), condition.get("message")
        )


async def fetch_related_flux_object(app: api.App, kind: str):
    """
    Returns the related Flux object of the given kind for the app.
    """
    ekapi = ekclient.api(
        "helm.toolkit.fluxcd.io/v2"
        if kind == "HelmRelease"
        else "source.toolkit.fluxcd.io/v1"
    )
    ekresource = await ekapi.resource(kind)
    obj = await ekresource.first(
        labels={"apps.azimuth-cloud.io/app": app.metadata.name},
        namespace=app.metadata.namespace,
    )
    if obj:
        return obj
    else:
        raise kopf.TemporaryError(f"{kind} does not exist for app")


def reconcile_app_phase(
    repository: dict[str, t.Any], chart: dict[str, t.Any], release: dict[str, t.Any]
):
    """
    Reconcile the phase of an app. Returns a tuple of (phase, failure message).
    """
    repository_ready = extract_condition(repository, "Ready")
    repository_stalled = extract_condition(repository, "Stalled")
    chart_ready = extract_condition(chart, "Ready")
    chart_stalled = extract_condition(chart, "Stalled")
    ready = extract_condition(release, "Ready")
    released = extract_condition(release, "Released")
    reconciling = extract_condition(release, "Reconciling")

    # For the repository and chart, we only transition into the failed phase if they are
    # stalled. This means that Flux has identified that they cannot proceed without
    # changes to the spec. Anything else is considered preparing
    if repository_ready.status != "True":
        if repository_stalled.status == "True":
            return api.AppPhase.FAILED, str(repository_stalled)
        else:
            return api.AppPhase.PREPARING, None
    if chart_ready.status != "True":
        if chart_stalled.status == "True":
            return api.AppPhase.FAILED, str(chart_stalled)
        else:
            return api.AppPhase.PREPARING, None

    # If we get to here, the repository and chart are ready
    if ready.status == "True":
        return api.AppPhase.DEPLOYED, None
    elif reconciling.status == "True":
        # Whether reconciling resolves to installing or upgrading depends on whether
        # there has been a successful release previously
        if released.status == "True":
            return api.AppPhase.UPGRADING, None
        else:
            return api.AppPhase.INSTALLING, None
    elif ready.status == "False":
        # If the ready status is actually False rather than Unknown,
        # mark the app as failed
        return api.AppPhase.FAILED, str(ready)
    else:
        return api.AppPhase.PENDING, None


async def reconcile_app_status(
    app: api.App,
    repository: dict[str, t.Any],
    chart: dict[str, t.Any],
    release: dict[str, t.Any],
):
    """
    Reconcile the status of an app and a Flux object.
    """
    # If the app is deleting, we don't do anything
    if app.metadata.deletion_timestamp:
        return

    # Derive the phase of the app from the Flux objects
    app.status.phase, app.status.failure_message = reconcile_app_phase(
        repository, chart, release
    )

    # Get the notes and Zenith services directly from the Helm release, as the notes and
    # manifest are not exposed as part of the Flux HelmRelease object
    try:
        async with clients_for_app(app) as (ekclient_target, helm_client_target):
            revision = await get_helm_revision(helm_client_target, release)
            app.status.usage = revision.notes
            app.status.services = await get_zenith_services(ekclient_target, revision)
    finally:
        # Save the changes to the status
        await save_instance_status(app)
    # Make sure the identity platform that produces credentials for Zenith is up-to-date
    await update_identity_platform(app)


@model_handler(
    api.App,
    kopf.on.timer,
    interval=settings.timer_interval,
    idle=settings.timer_interval,
)
async def reconcile_app_status_timer(instance: api.App, **kwargs):
    """
    Reconcile the app status.
    """
    repository = await fetch_related_flux_object(instance, "HelmRepository")
    chart = await fetch_related_flux_object(instance, "HelmChart")
    release = await fetch_related_flux_object(instance, "HelmRelease")
    await reconcile_app_status(instance, repository, chart, release)


async def find_app(obj):
    """
    Returns the app associated with a Flux object.
    """
    apps = await ekresource_for_model(api.App)
    try:
        app_data = await apps.fetch(
            obj["metadata"]["labels"]["apps.azimuth-cloud.io/app"],
            namespace=obj["metadata"]["namespace"],
        )
    except easykube.ApiError as exc:
        if exc.status_code == 404:
            return None
        else:
            raise
    else:
        return api.App.model_validate(app_data)


@kopf.on.event(
    "helmrepositories.source.toolkit.fluxcd.io",
    labels={"apps.azimuth-cloud.io/app": kopf.PRESENT},
)
async def handle_helmrepository_event(body, **kwargs):
    """
    Handles changes to Flux HelmRepository objects associated with apps.
    """
    app = await find_app(body)
    if app:
        repository = body
        chart = await fetch_related_flux_object(app, "HelmChart")
        release = await fetch_related_flux_object(app, "HelmRelease")
        await reconcile_app_status(app, repository, chart, release)


@kopf.on.event(
    "helmcharts.source.toolkit.fluxcd.io",
    labels={"apps.azimuth-cloud.io/app": kopf.PRESENT},
)
async def handle_helmchart_event(body, **kwargs):
    """
    Handles changes to Flux HelmChart objects associated with apps.
    """
    app = await find_app(body)
    if app:
        repository = await fetch_related_flux_object(app, "HelmRepository")
        chart = body
        release = await fetch_related_flux_object(app, "HelmRelease")
        await reconcile_app_status(app, repository, chart, release)


@kopf.on.event(
    "helmreleases.helm.toolkit.fluxcd.io",
    labels={"apps.azimuth-cloud.io/app": kopf.PRESENT},
)
async def handle_helmrelease_event(body, **kwargs):
    """
    Handles changes to Flux HelmRelease objects associated with apps.
    """
    app = await find_app(body)
    if app:
        repository = await fetch_related_flux_object(app, "HelmRepository")
        chart = await fetch_related_flux_object(app, "HelmChart")
        release = body
        await reconcile_app_status(app, repository, chart, release)
