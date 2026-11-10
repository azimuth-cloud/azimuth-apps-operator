# Contributing

We welcome contributions and suggestions for improvements to this code base.
Please check for relevant issues and PRs before opening a new one of your own.

## Making a contribution

### Running CI tests

PRs raised from a fork can run the unit tests and linting but will fail to execute the publish and deploy tests due to missing secrets.
Outside contributors should raise a PR against a feature branch and not the default branch.
A maintainer will review the PR and once satisfied will merge the PR into the internal feature branch, from there a second PR will execute the deployment tests and merge into the default branch.

### Helm template snapshots

The CI in this repository uses the Helm
[unittest](https://github.com/helm-unittest/helm-unittest) plugin's
snapshotting functionality to check PRs for changes to the templated manifests.
Therefore, if your PR makes changes to the manifest templates or values, you
will need to update the saved snapshots to allow your changes to pass the
automated tests. The easiest way to do this is to run the `helm unittest` command
inside a docker container from the repo root.

```
docker run -i --rm -v $(pwd):/apps helmunittest/helm-unittest chart -u
```

where the `-u` option is used to update the existing snapshots.

### Python scripts

The CI in this repository lints and checks python on PRs using `tox`.
For development, run `$ tox` in the root of the repo.
