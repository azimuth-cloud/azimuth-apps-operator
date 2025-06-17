from easysemver import SEMVER_VERSION_REGEX
from kube_custom_resource import CustomResource, schema
from pydantic import Field


class AppTemplateVersionRef(schema.BaseModel):
    """
    The spec for the app template and version to use.
    """

    name: schema.constr(min_length=1) = Field(
        ..., description="The name of the template to use."
    )
    version: schema.constr(pattern=SEMVER_VERSION_REGEX) = Field(
        ..., description="The version of the template to use."
    )


class KubeconfigSecret(schema.BaseModel):
    """
    The spec for the kubeconfig secret reference.
    """

    name: schema.constr(min_length=1) = Field(
        ..., description="The name of the secret containing the kubeconfig."
    )
    key: schema.constr(min_length=1) = Field(
        ..., description="The key in the secret containing the kubeconfig."
    )


class AppSpec(schema.BaseModel):
    """
    The spec for an Azimuth Kubernetes app.
    """

    template: AppTemplateVersionRef = Field(
        ..., description="The template and version to use for the app."
    )
    kubeconfig_secret: KubeconfigSecret = Field(
        ...,
        description=(
            "The secret name and key containing the kubeconfig to use "
            "to deploy the app."
        ),
    )
    zenith_identity_realm_name: schema.constr(min_length=1) = Field(
        ..., description="The identity realm to use for authenticating Zenith services."
    )
    values: schema.Dict[str, schema.Any] = Field(
        default_factory=dict, description="The values to use for the app."
    )
    created_by_username: schema.constr(min_length=1) = Field(
        ...,
        description="Username of user that created the app.",
    )
    created_by_user_id: schema.constr(min_length=1) = Field(
        ...,
        description="User id of user that created the app.",
    )
    updated_by_username: schema.Optional[schema.constr(min_length=1)] = Field(
        None,
        description="Username of user that updated the app.",
    )
    updated_by_user_id: schema.Optional[schema.constr(min_length=1)] = Field(
        None,
        description="User id of user that updated the app.",
    )


class AppPhase(str, schema.Enum):
    """
    The phase of the app.
    """

    #: Indicates that the state of the app is not known
    UNKNOWN = "Unknown"
    #: Indicates that the app is waiting to be deployed
    PENDING = "Pending"
    #: Indicates that the app is taking some action to prepare for an install/upgrade
    PREPARING = "Preparing"
    #: Indicates that the app deployed successfully
    DEPLOYED = "Deployed"
    #: Indicates that the app failed to deploy
    FAILED = "Failed"
    #: Indicates that the app is installing
    INSTALLING = "Installing"
    #: Indicates that the app is upgrading
    UPGRADING = "Upgrading"
    #: Indicates that the app is uninstalling
    UNINSTALLING = "Uninstalling"


class AppServiceStatus(schema.BaseModel):
    """
    The status of a Zenith service for an app.
    """

    subdomain: str = Field(..., description="The subdomain for the service.")
    fqdn: str = Field(..., description="The FQDN for the service.")
    label: str = Field(..., description="The label for the service.")
    icon_url: schema.Optional[str] = Field(
        None, description="The URL of the icon for the service."
    )
    description: schema.Optional[str] = Field(
        None, description="The description of the service."
    )


class AppStatus(schema.BaseModel, extra="allow"):
    """
    The status of the Kubernetes app.
    """

    phase: AppPhase = Field(AppPhase.UNKNOWN.value, description="The phase of the app.")
    usage: schema.Optional[str] = Field(
        None, description="Usage information for the app."
    )
    failure_message: schema.Optional[str] = Field(
        None, description="If known, the reason for the app entering the failed phase."
    )
    services: schema.Dict[str, AppServiceStatus] = Field(
        default_factory=dict, description="The Zenith services for the app."
    )


class App(
    CustomResource,
    subresources={"status": {}},
    printer_columns=[
        {
            "name": "Template name",
            "type": "string",
            "jsonPath": ".spec.template.name",
        },
        {
            "name": "Template version",
            "type": "string",
            "jsonPath": ".spec.template.version",
        },
        {
            "name": "Kubeconfig secret",
            "type": "string",
            "jsonPath": ".spec.kubeconfigSecret.name",
        },
        {
            "name": "Created by",
            "type": "string",
            "jsonPath": ".spec.createdByUsername",
        },
        {
            "name": "Updated by",
            "type": "string",
            "jsonPath": ".spec.updatedByUsername",
        },
        {
            "name": "Phase",
            "type": "string",
            "jsonPath": ".status.phase",
        },
    ],
):
    """
    An Azimuth Kubernetes app.
    """

    spec: AppSpec
    status: AppStatus = Field(default_factory=AppStatus)
