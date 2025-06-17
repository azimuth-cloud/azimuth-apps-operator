import asyncio
import datetime
import functools

import easykube
from aiohttp import web

from .config import settings


class Metric:
    # The prefix for the metric
    prefix = None
    # The suffix for the metric
    suffix = None
    # The type of the metric - info or gauge
    type = "info"
    # The description of the metric
    description = None

    def __init__(self):
        self._objs = []

    def add_obj(self, obj):
        self._objs.append(obj)

    @property
    def name(self):
        return f"{self.prefix}_{self.suffix}"

    def labels(self, obj):
        """The labels for the given object."""
        return {**self.common_labels(obj), **self.extra_labels(obj)}

    def common_labels(self, obj):
        """Common labels for the object."""
        return {}

    def extra_labels(self, obj):
        """Extra labels for the object."""
        return {}

    def value(self, obj):
        """The value for the given object."""
        return 1

    def records(self):
        """Returns the records for the metric, i.e. a list of (labels, value) tuples."""
        for obj in self._objs:
            yield self.labels(obj), self.value(obj)


class AppTemplateMetric(Metric):
    prefix = "azimuth_apps_template"

    def common_labels(self, obj):
        return {"template_name": obj.metadata.name}


class AppTemplateInfo(AppTemplateMetric):
    suffix = "info"
    description = "Basic info for the app template"

    def extra_labels(self, obj):
        return {"chart_repo": obj.spec.chart.repo, "chart_name": obj.spec.chart.name}


class AppTemplateLastSync(AppTemplateMetric):
    suffix = "last_sync"
    type = "gauge"
    description = "The time of the last sync for the app template"

    def value(self, obj):
        last_sync = obj.get("status", {}).get("lastSync")
        return as_timestamp(last_sync) if last_sync else 0


class AppTemplateLatestVersion(AppTemplateMetric):
    suffix = "latest_version"
    description = "The latest version for the template"

    def extra_labels(self, obj):
        versions = obj.get("status", {}).get("versions")
        if versions:
            version = versions[0]["name"]
        else:
            version = "0.0.0"
        return {"version": version}


class AppTemplateVersion(AppTemplateMetric):
    suffix = "version"
    description = "The versions supported by each template"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            for version in obj.get("status", {}).get("versions", []):
                yield {**labels, "version": version["name"]}, 1


class AppMetric(Metric):
    prefix = "azimuth_apps_app"

    def common_labels(self, obj):
        return {
            "app_namespace": obj.metadata.namespace,
            "app_name": obj.metadata.name,
        }


class AppInfo(AppMetric):
    suffix = "info"
    description = "Information about the app"

    def extra_labels(self, obj):
        return {
            "template_name": obj.spec.template.name,
            "template_version": obj.spec.template.version,
            "kubeconfig_secret_name": obj.spec["kubeconfigSecret"]["name"],
        }


class AppPhase(AppMetric):
    suffix = "phase"
    description = "App phase"

    def extra_labels(self, obj):
        return {"phase": obj.get("status", {}).get("phase", "Unknown")}


class AppService(AppMetric):
    suffix = "service"
    description = "The services for an app"

    def records(self):
        for obj in self._objs:
            labels = super().labels(obj)
            services = obj.get("status", {}).get("services", {})
            for service_name, service in services.items():
                yield (
                    {
                        **labels,
                        "service_name": service_name,
                        "service_subdomain": service["subdomain"],
                        "service_fqdn": service["fqdn"],
                    },
                    1,
                )


def escape(content):
    """Escape the given content for use in metric output."""
    return content.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def as_timestamp(datetime_str):
    """Converts a datetime string to a timestamp."""
    dt = datetime.datetime.fromisoformat(datetime_str)
    return round(dt.timestamp())


def format_value(value):
    """Formats a value for output, e.g. using Go formatting."""
    formatted = repr(value)
    dot = formatted.find(".")
    if value > 0 and dot > 6:
        mantissa = f"{formatted[0]}.{formatted[1:dot]}{formatted[dot + 1 :]}".rstrip(
            "0."
        )
        return f"{mantissa}e+0{dot - 1}"
    else:
        return formatted


def render_openmetrics(*metrics):
    """Renders the metrics using OpenMetrics text format."""
    output = []
    for metric in metrics:
        if metric.description:
            output.append(f"# HELP {metric.name} {escape(metric.description)}\n")
        output.append(f"# TYPE {metric.name} {metric.type}\n")

        for labels, value in metric.records():
            if labels:
                labelstr = "{{{}}}".format(
                    ",".join([f'{k}="{escape(v)}"' for k, v in sorted(labels.items())])
                )
            else:
                labelstr = ""
            output.append(f"{metric.name}{labelstr} {format_value(value)}\n")
    output.append("# EOF\n")

    return (
        "application/openmetrics-text; version=1.0.0; charset=utf-8",
        "".join(output).encode("utf-8"),
    )


METRICS = {
    settings.api_group: {
        "apptemplates": [
            AppTemplateInfo,
            AppTemplateLastSync,
            AppTemplateLatestVersion,
            AppTemplateVersion,
        ],
        "apps": [
            AppInfo,
            AppPhase,
            AppService,
        ],
    },
}


async def metrics_handler(ekclient, request):
    """Produce metrics for the operator."""
    metrics = []
    for api_group, resources in METRICS.items():
        ekapi = await ekclient.api_preferred_version(api_group)
        for resource, metric_classes in resources.items():
            ekresource = await ekapi.resource(resource)
            resource_metrics = [klass() for klass in metric_classes]
            async for obj in ekresource.list(all_namespaces=True):
                for metric in resource_metrics:
                    metric.add_obj(obj)
            metrics.extend(resource_metrics)

    content_type, content = render_openmetrics(*metrics)
    return web.Response(headers={"Content-Type": content_type}, body=content)


async def metrics_server():
    """Launch a lightweight HTTP server to serve the metrics endpoint."""
    ekclient = easykube.Configuration.from_environment().async_client()

    app = web.Application()
    app.add_routes([web.get("/metrics", functools.partial(metrics_handler, ekclient))])

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", "8080", shutdown_timeout=1.0)
    await site.start()

    # Sleep until we need to clean up
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.shield(runner.cleanup())
