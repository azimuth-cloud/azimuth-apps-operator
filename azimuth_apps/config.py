from pydantic import Field

from configomatic import Configuration as BaseConfiguration, LoggingConfiguration


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


settings = Configuration()
