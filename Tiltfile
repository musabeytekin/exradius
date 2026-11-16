# Exradius on the local kind cluster (docs/kubernetes.md §17).
#
#   make -C deploy/kind dev-up    # once: registry, cluster, operators, Secrets
#   tilt up                       # build every image, install the chart, live-update Python
#   tilt up -- --vault            # the same with the local Vault (make -C deploy/kind dev-vault)
#   tilt down                     # uninstall (the exradius namespace and its Secrets stay)
#
# Panel http://localhost:3100, API http://localhost:8100, RabbitMQ localhost:5673 and its
# UI http://localhost:15673: ports beside the v0.1.0 compose stack's. Exradius Desk
# http://localhost:3200 with its dev identity provider (make -C deploy/kind dev-desk) at
# http://localhost:8180; Desk starts once `make -C deploy/kind desk-token` has run.

CONTEXT = 'kind-exradius'
allow_k8s_contexts(CONTEXT)
if k8s_context() != CONTEXT:
    fail('kubectl context is %r, not %r: run `make -C deploy/kind dev-up`' % (k8s_context(), CONTEXT))

update_settings(k8s_upsert_timeout_secs=120)
# Tilt's own pruning removes every Docker build cache older than six hours, other
# projects' too.
docker_prune_settings(disable=True)


def package_version():
    for line in str(read_file('backend/pyproject.toml')).splitlines():
        if line.startswith('version = '):
            return line.split('"')[1]
    fail('no version in backend/pyproject.toml')


# -- images --------------------------------------------------------------------------------

BACKEND_ONLY = ['backend/pyproject.toml', 'backend/uv.lock', 'backend/alembic.ini', 'backend/exradius']
PYTHON_IGNORE = ['**/__pycache__', '**/*.pyc']

# The backend image as built for releases, then a development layer with Tilt's restart
# wrapper (the chart runs the api and the worker under it with dev.restartWrapper): a
# change to a Python file is synced into the running containers and restarts the process.
docker_build(
    'exradius-backend-base', '.',
    dockerfile='deploy/backend.Dockerfile',
    only=BACKEND_ONLY,
    ignore=PYTHON_IGNORE,
)
docker_build(
    'exradius-backend', 'deploy/kind',
    dockerfile_contents='''
FROM tiltdev/restart-helper:2026-10-21 AS restart-helper
FROM exradius-backend-base
COPY --from=restart-helper /tilt-restart-wrapper /entr /
RUN touch /opt/exradius/.restart-proc
''',
    only=['cluster.yaml'],
    live_update=[
        fall_back_on(['backend/pyproject.toml', 'backend/uv.lock']),
        sync('backend/exradius', '/opt/exradius/exradius'),
        run('date > /opt/exradius/.restart-proc'),
    ],
)

docker_build('exradius-frontend', 'frontend', ignore=['node_modules', 'dist', 'coverage'])

docker_build('exradius-operator', '.', dockerfile='deploy/operator.Dockerfile', only=['operator'],
             ignore=['operator/bin', 'operator/testbin'])

docker_build('exradius-egress', '.', dockerfile='deploy/egress.Dockerfile', only=['egress'], ignore=['egress/bin'])

# Build pods' image (rootless BuildKit with the clouds' credential helpers): the operator
# reads it from EXRADIUS_BUILDER_IMAGE.
docker_build('exradius-builder', '.', dockerfile='deploy/builder.Dockerfile', only=['builder'],
              ignore=['builder/bin'], match_in_env_vars=True)

# Not run by any Deployment: the worker names them in run pods, so Tilt writes the pushed
# references into its environment (EXRADIUS_RUNTIME_IMAGE_CLAUDE_CODE,
# EXRADIUS_PROJECT_DEFAULT_IMAGE).
docker_build(
    'exradius-runtime-claude-code', '.',
    dockerfile='deploy/runtime-claude-code.Dockerfile',
    build_args={'EXRADIUS_VERSION': package_version()},
    only=['backend/pyproject.toml', 'backend/uv.lock', 'backend/exradius', 'deploy/runtime-claude-code'],
    ignore=PYTHON_IGNORE,
    match_in_env_vars=True,
)
# Exradius Desk (bridges/desk): the kit comes in as a named build context, which
# docker_build cannot pass.
custom_build(
    'exradius-desk',
    'docker build --build-context kit=bridges/kit -t $EXPECTED_REF bridges/desk',
    deps=['bridges/desk/pyproject.toml', 'bridges/desk/uv.lock', 'bridges/desk/Dockerfile',
          'bridges/desk/exradius_desk', 'bridges/desk/web/src', 'bridges/desk/web/index.html',
          'bridges/desk/web/package.json', 'bridges/desk/web/package-lock.json',
          'bridges/kit/pyproject.toml', 'bridges/kit/exradius_bridge_kit'],
    ignore=PYTHON_IGNORE + ['bridges/desk/exradius_desk/static'],
)
docker_build(
    'exradius-project-default', 'deploy',
    dockerfile='deploy/project-default.Dockerfile',
    only=['project-default.Dockerfile'],
    match_in_env_vars=True,
)

# -- manifests -----------------------------------------------------------------------------

# `tilt up -- --vault` adds deploy/kind/values-vault.yaml: the local Vault of
# `make -C deploy/kind dev-vault`, for stores of kind vault (docs/kubernetes.md §17).
config.define_bool('vault', usage='install with deploy/kind/values-vault.yaml (run make -C deploy/kind dev-vault first)')
tilt_config = config.parse()
chart_values = ['charts/exradius/values-local.yaml']
if tilt_config.get('vault', False):
    chart_values.append('deploy/kind/values-vault.yaml')

k8s_yaml('deploy/kind/namespace.yaml')
k8s_yaml(helm(
    'charts/exradius',
    name='exradius',
    namespace='exradius',
    values=chart_values,
    # Bare image names, which Tilt replaces with the images it pushed.
    set=['images.registry=', 'dev.restartWrapper=true'],
))

k8s_kind('Cluster', api_version='postgresql.cnpg.io/v1', pod_readiness='wait')
k8s_kind('RabbitmqCluster', api_version='rabbitmq.com/v1beta1', pod_readiness='wait')

k8s_resource(
    new_name='setup',
    objects=[
        'exradius:namespace',
        'exradius-runs:namespace',
        'exradius-builds:namespace',
        'runs.exradius.dev:customresourcedefinition',
        'builds.exradius.dev:customresourcedefinition',
        'exradius-run-identity:configmap',
        'exradius-buildkitd:configmap',
        'exradius-api:serviceaccount',
        'exradius-frontend:serviceaccount',
        'exradius-worker:serviceaccount',
        'exradius-operator:serviceaccount',
        'exradius-run:serviceaccount',
        'exradius-build:serviceaccount',
        'exradius-worker:role:exradius',
        'exradius-worker:role:exradius-runs',
        'exradius-worker:role:exradius-builds',
        'exradius-worker:rolebinding:exradius',
        'exradius-worker:rolebinding:exradius-runs',
        'exradius-worker:rolebinding:exradius-builds',
        'exradius-operator:role:exradius',
        'exradius-operator:role:exradius-runs',
        'exradius-operator:role:exradius-builds',
        'exradius-operator:rolebinding:exradius',
        'exradius-operator:rolebinding:exradius-runs',
        'exradius-operator:rolebinding:exradius-builds',
        'exradius-egress:serviceaccount',
        'exradius-egress:role:exradius-runs',
        'exradius-egress:role:exradius-builds',
        'exradius-egress:rolebinding:exradius-runs',
        'exradius-egress:rolebinding:exradius-builds',
        'exradius-exradius-api-auth:clusterrolebinding',
        'exradius-run:networkpolicy:exradius-runs',
        'exradius-build:networkpolicy:exradius-builds',
        'exradius-api:networkpolicy',
        'exradius-egress:networkpolicy',
        'exradius-desk:serviceaccount',
    ],
    labels=['stack'],
)
k8s_resource('exradius-postgres', resource_deps=['setup'], labels=['stack'])
k8s_resource('exradius-rabbitmq', resource_deps=['setup'], labels=['stack'],
             port_forwards=['5673:5672', '15673:15672'])
k8s_resource('exradius-seaweedfs', objects=['exradius-seaweedfs:persistentvolumeclaim'],
             resource_deps=['setup'], labels=['stack'])

STACK = ['setup', 'exradius-postgres', 'exradius-rabbitmq', 'exradius-seaweedfs']
k8s_resource('exradius-api', resource_deps=STACK, labels=['exradius'], port_forwards=['8100:8000'])
k8s_resource('exradius-worker', resource_deps=STACK, labels=['exradius'])
k8s_resource('exradius-operator', resource_deps=['setup'], labels=['exradius'])
k8s_resource('exradius-egress', resource_deps=['setup'], labels=['exradius'])
k8s_resource('exradius-frontend', resource_deps=['exradius-api'], labels=['exradius'],
             port_forwards=['3100:8080'])

# Exradius Desk (bridges/desk/docs/design.md §14): its database, the service, and Keycloak's
# port-forward once make -C deploy/kind dev-desk has installed it.
k8s_resource('exradius-desk-postgres', resource_deps=['setup'], labels=['desk'])
k8s_resource('exradius-desk', resource_deps=['exradius-desk-postgres', 'exradius-rabbitmq', 'exradius-api'],
             labels=['desk'], port_forwards=['3200:8080'])
local_resource(
    'keycloak',
    serve_cmd=('until kubectl --context %s -n keycloak get svc keycloak >/dev/null 2>&1; do sleep 5; done; ' +
               'exec kubectl --context %s -n keycloak port-forward svc/keycloak 8180:8080') % (CONTEXT, CONTEXT),
    labels=['desk'],
)
