# azimuth-apps-operator

This operator has an app template and app CRD.

Zenith operators are also attached to watch
cluster kubeconfigs. It finds the secrets
by looking for secrets with the label:

```yaml
labels:
  "apps.azimuth-cloud.io/default-kubeconfig": "whatever"
```

Note the Zenith operator installs the Zenith
reservation and client CRDs into the target cluster,
allowing the target cluster to register
Zenith services.

When you create an instance of the App CRD
we created FluxCD resources to manage the
helm release on the target cluster,
as described by the app template.

Note this operator is looking for Zenith
reservation CRDs that are in the above
helm release.
When it finds a Zenith reservation, we track
the state of those, and make the links to them
available in the status of the App CRD.

To manage credentials for the Zenith service,
we also create instances of the identity operator's
Platform CRD.
This is required for any Zenith service
that needs OIDC auth.
Zenith sync service waits for the identity
operator to create the secrets it needs for
OIDC, triggered by the Platform CRD we manage.

Dummy change to test CI.
