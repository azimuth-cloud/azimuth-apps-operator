# azimuth-apps-operator

This operator has an app template and app CRD.

Zenith operators are also attached to watch
cluster kubeconfigs. It finds the secrets
by looking for secrets with the label:
```yaml
labels:
  "apps.azimuth-cloud.io/default-kubeconfig": "whatever"
```

When you create an instance of the App CRD
we created FluxCD resources to manage the 
helm release on the target cluster,
as described by the app template.
