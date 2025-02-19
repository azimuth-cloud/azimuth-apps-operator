import typing as t

from pydantic import Field, conint, constr

from configomatic import Configuration as BaseConfiguration, Section, LoggingConfiguration


class HelmClientConfiguration(Section):
    """
    Configuration for the Helm client.
    """
    #: The default timeout to use with Helm releases
    #: Can be an integer number of seconds or a duration string like 5m, 5h
    default_timeout: t.Union[conint(gt = 0), constr(min_length = 1)] = "1h"
    #: The executable to use
    #: By default, we assume Helm is on the PATH
    executable: constr(min_length = 1) = "helm"
    #: The maximum number of revisions to retain in the history of releases
    history_max_revisions: int = 10
    #: Indicates whether to verify TLS when pulling charts
    insecure_skip_tls_verify: bool = False
    #: The directory to use for unpacking charts
    #: By default, the system temporary directory is used
    unpack_directory: t.Optional[str] = None


class Configuration(
    BaseConfiguration,
    default_path = "/etc/azimuth/apps-operator.yaml",
    path_env_var = "AZIMUTH_APPS_CONFIG",
    env_prefix = "AZIMUTH_APPS"
):
    """
    Top-level configuration model.
    """
    #: The logging configuration
    logging: LoggingConfiguration = Field(default_factory = LoggingConfiguration)

    #: The API group of the cluster CRDs
    api_group: constr(min_length = 1) = "apps.azimuth-cloud.io"
    #: A list of categories to place CRDs into
    crd_categories: t.List[constr(min_length = 1)] = Field(
        default_factory = lambda: ["azimuth", "apps", "azimuth-apps"]
    )

    #: The field manager name to use for server-side apply
    easykube_field_manager: constr(min_length = 1) = "azimuth-apps-operator"

    #: The amount of time (seconds) before a watch is forcefully restarted
    watch_timeout: conint(gt = 0) = 600

    #: The number of seconds to wait between timer executions
    timer_interval: conint(gt = 0) = 60

    #: The Helm client configuration
    helm_client: HelmClientConfiguration = Field(default_factory = HelmClientConfiguration)


settings = Configuration()
