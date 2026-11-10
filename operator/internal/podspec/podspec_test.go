package podspec

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/yaml"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
)

var update = flag.Bool("update", false, "rewrite the golden files")

func config() Config {
	return Config{
		APIURL:               "http://exradius-api-internal.exradius:8001",
		RunUID:               1000,
		RunGID:               1000,
		IdentityConfigMap:    "exradius-run-identity",
		ServiceAccount:       "exradius-run",
		WorkspaceSize:        resource.MustParse("50Gi"),
		MemoryRequest:        resource.MustParse("1Gi"),
		CPURequest:           resource.MustParse("500m"),
		ServiceMemoryRequest: resource.MustParse("256Mi"),
		ServiceCPURequest:    resource.MustParse("100m"),
		ProjectVolume: ProjectVolumeConfig{
			Enabled: true, AccessMode: corev1.ReadWriteOnce, Size: resource.MustParse("100Gi"),
		},
		Egress: EgressConfig{
			Proxy: "http://exradius-egress.exradius:3128",
			URL:   "http://exradius-egress.exradius:3129",
			Peers: "exradius-egress-pods.exradius",
		},
	}
}

func baseRun(purpose v1.Purpose, name, id string) *v1.Run {
	return &v1.Run{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "exradius-runs", UID: types.UID("uid-" + id)},
		Spec: v1.RunSpec{
			Purpose:    purpose,
			ID:         id,
			SecretName: name,
			Runtime:    v1.RuntimeSpec{Name: "claude_code", Image: "kind-registry:5000/exradius-runtime-claude-code:0.2.0"},
			Workstation: v1.WorkstationSpec{
				Image: "kind-registry:5000/exradius-project-default:0.2.0",
			},
		},
	}
}

func taskRun() *v1.Run {
	run := baseRun(v1.PurposeTask, "run-3f2c", "3f2c")
	memory := resource.MustParse("6Gi")
	run.Spec.Project = "saleor"
	run.Spec.TaskID = "SAL-101"
	run.Spec.PodGeneration = 1
	run.Spec.Runtime.Env = []v1.EnvVar{
		{Name: "ANTHROPIC_BASE_URL", Value: "http://exradius-egress.exradius:3129/anthropic"},
		{Name: "ANTHROPIC_API_KEY", Value: "placeholder"},
	}
	run.Spec.Workstation.Setup = []string{"uv sync", "make migrate"}
	run.Spec.Workstation.Memory = &memory
	run.Spec.Workstation.Env = []v1.EnvVar{
		{Name: "DJANGO_SETTINGS_MODULE", Value: "saleor.settings"},
		{Name: "DATABASE_URL", SecretKey: "env.DATABASE_URL"},
		{Name: "HOME", Value: "/root"},                   // replaced by the operator's HOME
		{Name: "HTTPS_PROXY", Value: "http://evil:3128"}, // replaced by the operator's proxy
		{Name: "JAVA_TOOL_OPTIONS", Value: "-Xmx2g"},     // kept, before the proxy's options
	}
	run.Spec.Repositories = []v1.RepositorySpec{
		{Folder: "saleor", URL: "https://github.com/acme/saleor.git", Branch: "main", Push: true},
		{Folder: "Dashboard.UI", URL: "https://github.com/acme/dashboard.git"},
	}
	run.Spec.Workstation.Caches = []v1.CacheSpec{
		{Path: "/home/runner/.cache/uv", Name: "0-cache_uv"},
		{Path: "/opt/pnpm-store", Name: "1-opt_pnpm-store"},
	}
	dbMemory := resource.MustParse("512Mi")
	run.Spec.Services = []v1.ServiceSpec{
		{
			Name: "postgres", Image: "postgres:16", Ports: []int32{5432}, Memory: &dbMemory,
			Env: []v1.EnvVar{
				{Name: "POSTGRES_USER", Value: "saleor"},
				{Name: "POSTGRES_PASSWORD", SecretKey: "service.postgres.POSTGRES_PASSWORD"},
			},
			Ready: &v1.ReadySpec{Exec: []string{"pg_isready", "-h", "127.0.0.1", "-U", "saleor"}},
		},
		{Name: "cache", Image: "valkey/valkey:8.1-alpine", Ports: []int32{6379}, Ready: &v1.ReadySpec{TCP: 6379},
			Command: []string{"valkey-server", "--save", ""}},
		{Name: "mail", Image: "axllent/mailpit"},
	}
	run.Spec.Git = v1.GitSpec{
		SetupEnv: []v1.EnvVar{{Name: "GIT_CONFIG_COUNT", Value: "1"}, {Name: "GIT_CONFIG_KEY_0", Value: "safe.directory"}, {Name: "GIT_CONFIG_VALUE_0", Value: "*"}},
		Env: []v1.EnvVar{
			{Name: "GIT_AUTHOR_NAME", Value: "Developer"},
			{Name: "GIT_CONFIG_COUNT", Value: "1"},
			{Name: "GIT_CONFIG_KEY_0", Value: "url.http://exradius-egress.exradius:3129/git/github.com/acme/saleor.git.insteadOf"},
			{Name: "GIT_CONFIG_VALUE_0", Value: "https://github.com/acme/saleor.git"},
		},
	}
	return run
}

func checkGolden(t *testing.T, name string, objects ...any) {
	t.Helper()
	var out []byte
	for _, object := range objects {
		data, err := yaml.Marshal(object)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, []byte("---\n")...)
		out = append(out, data...)
	}
	path := filepath.Join("testdata", name+".yaml")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, out, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./internal/podspec -update)", err)
	}
	if string(want) != string(out) {
		t.Errorf("%s differs from the golden file; got:\n%s", path, out)
	}
}

func TestTaskPod(t *testing.T) {
	b := Pods{Config: config()}
	run := taskRun()
	checkGolden(t, "task", b.Workspace(run), b.ProjectVolume(run), b.Pod(run))
}

// Services are native sidecars after the clones, in file order, on localhost by name.
func TestServices(t *testing.T) {
	pod := Pods{Config: config()}.Pod(taskRun())
	var order []string
	for _, c := range pod.Spec.InitContainers {
		order = append(order, c.Name)
	}
	want := "prepare clone-0 clone-1 service-postgres service-cache service-mail setup-0 setup-1 files"
	if got := strings.Join(order, " "); got != want {
		t.Fatalf("init containers: %s", got)
	}
	if len(pod.Spec.HostAliases) != 1 || pod.Spec.HostAliases[0].IP != "127.0.0.1" ||
		strings.Join(pod.Spec.HostAliases[0].Hostnames, ",") != "postgres,cache,mail" {
		t.Fatalf("hostAliases: %+v", pod.Spec.HostAliases)
	}
	db, cache, mail := pod.Spec.InitContainers[3], pod.Spec.InitContainers[4], pod.Spec.InitContainers[5]
	if *db.RestartPolicy != corev1.ContainerRestartPolicyAlways || db.StartupProbe.Exec == nil ||
		db.StartupProbe.PeriodSeconds != 1 || db.StartupProbe.FailureThreshold < 120 {
		t.Fatalf("postgres: %+v", db)
	}
	if !db.Resources.Requests.Memory().Equal(resource.MustParse("512Mi")) || !db.Resources.Limits.Memory().Equal(resource.MustParse("512Mi")) {
		t.Fatalf("declared memory is request and limit: %+v", db.Resources)
	}
	if cache.StartupProbe.TCPSocket == nil || cache.StartupProbe.TCPSocket.Port.IntValue() != 6379 || cache.Resources.Limits != nil ||
		!cache.Resources.Requests.Memory().Equal(resource.MustParse("256Mi")) {
		t.Fatalf("cache: %+v", cache)
	}
	if strings.Join(cache.Args, "|") != "valkey-server|--save|" || cache.Command != nil {
		t.Fatalf("a command keeps the entrypoint: command %v, args %v", cache.Command, cache.Args)
	}
	if mail.StartupProbe != nil {
		t.Fatal("no ready check: no startup probe")
	}
	for _, svc := range []corev1.Container{db, cache, mail} {
		sc := svc.SecurityContext
		if sc.RunAsUser != nil || sc.RunAsNonRoot != nil || *sc.ReadOnlyRootFilesystem || *sc.AllowPrivilegeEscalation ||
			len(sc.Capabilities.Add) != len(ServiceCapabilities) || len(svc.VolumeMounts) != 0 {
			t.Fatalf("%s: services keep their image's user, a writable root and no mounts: %+v", svc.Name, svc)
		}
	}
	if pod.Spec.SecurityContext.RunAsUser != nil || pod.Spec.SecurityContext.RunAsNonRoot != nil {
		t.Fatal("the run user is per container, not the pod's")
	}
}

// The project volume holds the mirrors (clones) and the caches (setup and workstation);
// prepare creates their directories as the run user.
func TestProjectVolume(t *testing.T) {
	b := Pods{Config: config()}
	run := taskRun()
	pvc := b.ProjectVolume(run)
	if pvc.Name != "exradius-project-saleor" || len(pvc.OwnerReferences) != 0 || pvc.Labels[LabelVolume] != "project" {
		t.Fatalf("project volume: %+v", pvc.ObjectMeta)
	}
	pod := b.Pod(run)
	mounts := func(name string) map[string]corev1.VolumeMount {
		out := map[string]corev1.VolumeMount{}
		for _, c := range append(append([]corev1.Container{}, pod.Spec.InitContainers...), pod.Spec.Containers...) {
			if c.Name == name {
				for _, m := range c.VolumeMounts {
					out[m.MountPath] = m
				}
			}
		}
		return out
	}
	if m := mounts("clone-0")[MirrorsDir]; m.Name != volProject || m.SubPath != "mirrors" {
		t.Fatalf("clone mounts the mirrors: %+v", m)
	}
	for _, container := range []string{"setup-0", ContainerWorkstation} {
		if m := mounts(container)["/home/runner/.cache/uv"]; m.Name != volProject || m.SubPath != "caches/0-cache_uv" {
			t.Fatalf("%s mounts the caches: %+v", container, m)
		}
	}
	if _, ok := mounts(ContainerFiles)["/home/runner/.cache/uv"]; ok {
		t.Fatal("the files sidecar has no caches")
	}
	if mounts(ContainerPrepare)[ProjectDir].SubPath != "" {
		t.Fatal("prepare mounts the whole project volume")
	}

	// Without the project volume: direct clones, and each cache is an empty directory on
	// the workspace.
	c := config()
	c.ProjectVolume.Enabled = false
	b = Pods{Config: c}
	if b.ProjectVolume(run) != nil {
		t.Fatal("no project volume when it is off")
	}
	pod = b.Pod(run)
	if _, ok := mounts("clone-0")[MirrorsDir]; ok {
		t.Fatal("no mirrors without a project volume")
	}
	if m := mounts(ContainerWorkstation)["/opt/pnpm-store"]; m.Name != volWorkspace || m.SubPath != "caches/1-opt_pnpm-store" {
		t.Fatalf("a cache on the workspace: %+v", m)
	}
	if b.ProjectVolume(baseRun(v1.PurposeProbe, "probe-1", "1")) != nil {
		t.Fatal("probes have no project volume")
	}
}

func TestVerifyPod(t *testing.T) {
	b := Pods{Config: config()}
	run := baseRun(v1.PurposeVerify, "verify-9a1b", "9a1b")
	run.Spec.Project = "saleor"
	run.Spec.Repositories = []v1.RepositorySpec{{Folder: "saleor", URL: "https://github.com/acme/saleor.git", Branch: "main"}}
	run.Spec.Workstation.Setup = []string{"uv sync"}
	pod := b.Pod(run)
	for _, c := range pod.Spec.InitContainers {
		if c.Name == ContainerFiles {
			t.Fatal("a verify pod has no files sidecar")
		}
	}
	checkGolden(t, "verify", b.Workspace(run), pod)
}

func TestProbePod(t *testing.T) {
	b := Pods{Config: config()}
	run := baseRun(v1.PurposeProbe, "probe-77aa", "77aa")
	run.Spec.Model = "claude-sonnet-5-5"
	run.Spec.Runtime.Env = []v1.EnvVar{{Name: "CLAUDE_CODE_OAUTH_TOKEN", SecretKey: "provider.CLAUDE_CODE_OAUTH_TOKEN"}}
	if b.Workspace(run) != nil {
		t.Fatal("a probe has no workspace")
	}
	pod := b.Pod(run)
	if len(pod.Spec.InitContainers) != 0 {
		t.Fatalf("a probe has no init containers, got %d", len(pod.Spec.InitContainers))
	}
	checkGolden(t, "probe", pod)
}

func TestPlacementAndIsolation(t *testing.T) {
	c := config()
	c.RuntimeClassName = "gvisor"
	c.UserNamespaces = true
	c.NodeSelector = map[string]string{"exradius.dev/pool": "runs"}
	c.Tolerations = []corev1.Toleration{{Key: "exradius.dev/runs", Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule}}
	c.ImagePullSecrets = []string{"registry"}
	c.WorkspaceStorageClass = "gp3"
	b := Pods{Config: c}
	run := baseRun(v1.PurposeTask, "run-5e5e", "5e5e")
	checkGolden(t, "placement", b.Workspace(run), b.Pod(run))
}

// The security context is the operator's, whatever the Run says.
func TestSecurityInvariants(t *testing.T) {
	b := Pods{Config: config()}
	pod := b.Pod(taskRun())
	if *pod.Spec.AutomountServiceAccountToken || *pod.Spec.EnableServiceLinks {
		t.Fatal("service account token and service links must be off")
	}
	if pod.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Fatal("restartPolicy must be Never")
	}
	for _, c := range append(append([]corev1.Container{}, pod.Spec.InitContainers...), pod.Spec.Containers...) {
		if strings.HasPrefix(c.Name, servicePrefix) {
			continue // TestServices
		}
		sc := c.SecurityContext
		if sc == nil || sc.RunAsUser == nil || !*sc.ReadOnlyRootFilesystem || *sc.AllowPrivilegeEscalation || !*sc.RunAsNonRoot ||
			len(sc.Capabilities.Drop) != 1 || sc.Capabilities.Drop[0] != "ALL" || len(sc.Capabilities.Add) != 0 {
			t.Fatalf("container %s: weak security context %+v", c.Name, sc)
		}
		names := map[string]bool{}
		for _, e := range c.Env {
			if names[e.Name] {
				t.Fatalf("container %s: duplicate variable %s", c.Name, e.Name)
			}
			names[e.Name] = true
		}
	}
	ws := pod.Spec.Containers[0]
	for _, e := range ws.Env {
		if e.Name == "HOME" && e.Value != HomeDir {
			t.Fatalf("HOME is %q", e.Value)
		}
	}
	if *pod.Spec.InitContainers[len(pod.Spec.InitContainers)-1].SecurityContext.RunAsUser != 1001 {
		t.Fatal("the files sidecar runs as a uid other than the run user's")
	}
}

func TestStepNames(t *testing.T) {
	steps := StepNames(taskRun())
	want := []string{"Prepare workspace", "Clone saleor (main)", "Clone Dashboard.UI (default branch)",
		"Start service postgres", "Start service cache", "Start service mail", "Setup: uv sync", "Setup: make migrate"}
	if len(steps) != len(want) {
		t.Fatalf("got %d steps", len(steps))
	}
	for i, s := range steps {
		if s.Name != want[i] || s.State != v1.StepPending {
			t.Errorf("step %d: %+v", i, s)
		}
	}
	if StepNames(baseRun(v1.PurposeProbe, "probe-1", "1")) != nil {
		t.Error("a probe has no steps")
	}
}

func buildConfig() BuildConfig {
	return BuildConfig{
		APIURL:             "http://exradius-api-internal.exradius:8001",
		Image:              "moby/buildkit:v0.34.1-rootless",
		ServiceAccount:     "exradius-build",
		BuildkitdConfigMap: "exradius-buildkitd",
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
		},
		Proxy:   "http://exradius-egress.exradius:3128",
		NoProxy: []string{"exradius-api-internal.exradius"},
	}
}

func build() *v1.Build {
	return &v1.Build{
		ObjectMeta: metav1.ObjectMeta{Name: "build-saleor-workstation-1a2b3c4d5e6f", Namespace: "exradius-builds", UID: "uid-b1"},
		Spec: v1.BuildSpec{
			ID: "b1", Project: "saleor", Name: "workstation",
			Image:      "kind-registry:5000/exradius/saleor/workstation:1a2b3c4d5e6f",
			Cache:      "kind-registry:5000/exradius/saleor/workstation:buildcache",
			Dockerfile: "docker/Dockerfile", SecretName: "build-saleor-workstation-1a2b3c4d5e6f",
		},
	}
}

func TestBuildPod(t *testing.T) {
	checkGolden(t, "build", buildConfig().BuildPod(build()))
}

func TestBuildPodWithoutCacheOrConfig(t *testing.T) {
	c := buildConfig()
	c.BuildkitdConfigMap = ""
	b := build()
	b.Spec.Cache = ""
	pod := c.BuildPod(b)
	if len(pod.Spec.Volumes) != 4 {
		t.Fatalf("expected the four emptyDirs only, got %d volumes", len(pod.Spec.Volumes))
	}
	for _, arg := range pod.Spec.Containers[0].Command {
		if arg == "--export-cache" || arg == "--import-cache" {
			t.Fatalf("no cache, but %s", arg)
		}
	}
	for _, e := range pod.Spec.Containers[0].Env {
		if e.Name == "BUILDKITD_FLAGS" && e.Value != "--oci-worker-no-process-sandbox" {
			t.Fatalf("BUILDKITD_FLAGS = %q", e.Value)
		}
	}
}

// Build pods carry no Kubernetes identity; only the build container is unconfined, and
// the token reaches the context fetch only.
func TestBuildPodSecurity(t *testing.T) {
	pod := buildConfig().BuildPod(build())
	if *pod.Spec.AutomountServiceAccountToken || *pod.Spec.EnableServiceLinks || pod.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Fatal("no service account token, no service links, never restarted")
	}
	ctx := pod.Spec.InitContainers[0]
	if sc := ctx.SecurityContext; !*sc.ReadOnlyRootFilesystem || *sc.AllowPrivilegeEscalation || sc.Capabilities.Drop[0] != "ALL" {
		t.Fatalf("context container: weak security context %+v", sc)
	}
	main := pod.Spec.Containers[0]
	if main.SecurityContext.SeccompProfile.Type != corev1.SeccompProfileTypeUnconfined || !*main.SecurityContext.ReadOnlyRootFilesystem {
		t.Fatalf("build container: %+v", main.SecurityContext)
	}
	for _, e := range main.Env {
		if e.ValueFrom != nil {
			t.Fatalf("the build container gets no secret, got %s", e.Name)
		}
	}
	if pod.OwnerReferences[0].Kind != "Build" || !*pod.OwnerReferences[0].Controller {
		t.Fatal("the pod is controlled by its Build")
	}
}

// With the node's Localhost profile the build container fits Pod Security baseline: no
// Unconfined seccomp or AppArmor, nothing privileged, no host namespaces or paths.
func TestBuildPodWithSeccompProfile(t *testing.T) {
	c := buildConfig()
	c.SeccompProfile = "exradius/buildkit.json"
	pod := c.BuildPod(build())
	sc := pod.Spec.Containers[0].SecurityContext
	if sc.SeccompProfile.Type != corev1.SeccompProfileTypeLocalhost || *sc.SeccompProfile.LocalhostProfile != "exradius/buildkit.json" {
		t.Fatalf("seccomp %+v", sc.SeccompProfile)
	}
	if sc.AppArmorProfile != nil || sc.Privileged != nil || (sc.Capabilities != nil && len(sc.Capabilities.Add) > 0) {
		t.Fatalf("beyond baseline: %+v", sc)
	}
	if pod.Spec.HostNetwork || pod.Spec.HostPID || pod.Spec.HostIPC {
		t.Fatal("host namespaces")
	}
	c.AppArmorProfile = "exradius-buildkit"
	sc = c.BuildPod(build()).Spec.Containers[0].SecurityContext
	if sc.AppArmorProfile == nil || sc.AppArmorProfile.Type != corev1.AppArmorProfileTypeLocalhost || *sc.AppArmorProfile.LocalhostProfile != "exradius-buildkit" {
		t.Fatalf("apparmor %+v", sc.AppArmorProfile)
	}
	for _, v := range pod.Spec.Volumes {
		if v.HostPath != nil {
			t.Fatalf("host path %s", v.Name)
		}
	}
}

// No run container holds a provider or repository credential: only the run token, the
// project's secret variables and the services' (docs/kubernetes.md §10).
func TestNoCredentialInRunPods(t *testing.T) {
	b := Pods{Config: config()}
	for _, run := range []*v1.Run{taskRun(), baseRun(v1.PurposeProbe, "probe-1", "1")} {
		pod := b.Pod(run)
		for _, c := range append(append([]corev1.Container{}, pod.Spec.InitContainers...), pod.Spec.Containers...) {
			for _, e := range c.Env {
				if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
					continue
				}
				key := e.ValueFrom.SecretKeyRef.Key
				if key != TokenKey && !strings.HasPrefix(key, "env.") && !strings.HasPrefix(key, "service.") {
					t.Errorf("%s: %s reads Secret key %s", c.Name, e.Name, key)
				}
			}
		}
	}
}

// The run token is never a variable (a process listing or `kubectl exec env` would show it):
// a file from the Secret where no project code runs, and in the workstation prepare's
// handoff, which the runner deletes (docs/kubernetes.md §7.8). Setup, services and clones
// do not get it at all.
func TestRunTokenIsAFile(t *testing.T) {
	b := Pods{Config: config()}
	for _, run := range []*v1.Run{taskRun(), baseRun(v1.PurposeVerify, "verify-1", "1"), baseRun(v1.PurposeProbe, "probe-1", "1")} {
		pod := b.Pod(run)
		for _, c := range append(append([]corev1.Container{}, pod.Spec.InitContainers...), pod.Spec.Containers...) {
			file := ""
			for _, e := range c.Env {
				if e.Name == "EXRADIUS_RUN_TOKEN" || (e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Key == TokenKey) {
					t.Fatalf("%s/%s: the token is a variable", run.Name, c.Name)
				}
				if e.Name == "EXRADIUS_RUN_TOKEN_FILE" {
					file = e.Value
				}
			}
			mounts := map[string]string{}
			for _, m := range c.VolumeMounts {
				mounts[m.Name] = m.MountPath
			}
			_, secret := mounts[volSecret]
			_, handoff := mounts[volToken]
			switch {
			case c.Name == ContainerWorkstation && run.Spec.Purpose != v1.PurposeProbe:
				if file != TokenDir+"/token" || secret || !handoff {
					t.Errorf("%s/%s: token file %q, secret %v, handoff %v", run.Name, c.Name, file, secret, handoff)
				}
			case c.Name == ContainerPrepare:
				if file != SecretDir+"/token" || !secret || !handoff {
					t.Errorf("%s/%s: token file %q, secret %v, handoff %v", run.Name, c.Name, file, secret, handoff)
				}
			case c.Name == ContainerFiles || c.Name == ContainerWorkstation:
				if file != SecretDir+"/token" || !secret || handoff {
					t.Errorf("%s/%s: token file %q, secret %v, handoff %v", run.Name, c.Name, file, secret, handoff)
				}
			default:
				if file != "" || secret || handoff {
					t.Errorf("%s/%s gets the token", run.Name, c.Name)
				}
			}
		}
		for _, v := range pod.Spec.Volumes {
			if v.Secret != nil && (len(v.Secret.Items) != 1 || v.Secret.Items[0].Key != TokenKey) {
				t.Errorf("%s: the Secret's volume holds more than the token", run.Name)
			}
		}
	}
}

// Every container goes through the proxy, reaches its services and the api directly, and
// the runner's containers know the proxy's routes.
func TestProxyVariables(t *testing.T) {
	pod := Pods{Config: config()}.Pod(taskRun())
	for _, c := range append(append([]corev1.Container{}, pod.Spec.InitContainers...), pod.Spec.Containers...) {
		env := map[string]string{}
		for _, e := range c.Env {
			env[e.Name] = e.Value
		}
		if env["HTTPS_PROXY"] != "http://exradius-egress.exradius:3128" || env["https_proxy"] != env["HTTPS_PROXY"] ||
			env["YARN_HTTPS_PROXY"] != env["HTTPS_PROXY"] {
			t.Errorf("%s: HTTPS_PROXY %q", c.Name, env["HTTPS_PROXY"])
		}
		for _, host := range []string{"localhost", "127.0.0.1", "postgres", "cache", "exradius-api-internal.exradius", "exradius-egress.exradius"} {
			if !strings.Contains(","+env["NO_PROXY"]+",", ","+host+",") {
				t.Errorf("%s: NO_PROXY %q lacks %s", c.Name, env["NO_PROXY"], host)
			}
		}
		runner := len(c.Command) > 0 && c.Command[0] == Runner && c.Name != ContainerPrepare && c.Name != ContainerFiles
		if runner != (env["EXRADIUS_EGRESS_URL"] != "") {
			t.Errorf("%s: EXRADIUS_EGRESS_URL %q", c.Name, env["EXRADIUS_EGRESS_URL"])
		}
		java := c.Name == ContainerWorkstation || strings.HasPrefix(c.Name, "setup-")
		if java != strings.Contains(env["JAVA_TOOL_OPTIONS"], "-Dhttps.proxyHost=exradius-egress.exradius") {
			t.Errorf("%s: JAVA_TOOL_OPTIONS %q", c.Name, env["JAVA_TOOL_OPTIONS"])
		}
	}
}

// Through the proxy: buildkitd's pulls and pushes by its environment, RUN steps by
// BuildKit's predefined proxy arguments; the context fetch goes to the api directly.
func TestBuildPodThroughTheProxy(t *testing.T) {
	pod := buildConfig().BuildPod(build())
	args := strings.Join(pod.Spec.Containers[0].Command, " ")
	for _, want := range []string{"--opt build-arg:HTTPS_PROXY=http://exradius-egress.exradius:3128",
		"--opt build-arg:no_proxy=localhost,127.0.0.1,::1,exradius-api-internal.exradius"} {
		if !strings.Contains(args, want) {
			t.Errorf("build args lack %q: %s", want, args)
		}
	}
	env := map[string]string{}
	for _, e := range pod.Spec.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if env["HTTPS_PROXY"] != "http://exradius-egress.exradius:3128" || env["NO_PROXY"] == "" {
		t.Errorf("build env %v", env)
	}
	for _, e := range pod.Spec.InitContainers[0].Env {
		if strings.HasSuffix(strings.ToUpper(e.Name), "_PROXY") {
			t.Errorf("the context fetch has %s", e.Name)
		}
	}
	direct := buildConfig()
	direct.Proxy = ""
	if strings.Contains(strings.Join(direct.BuildPod(build()).Spec.Containers[0].Command, " "), "build-arg") {
		t.Error("a direct build got proxy arguments")
	}
}
