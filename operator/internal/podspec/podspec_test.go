package podspec

import (
	"flag"
	"os"
	"path/filepath"
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
		APIURL:            "http://exradius-api-internal.exradius:8001",
		RunUID:            1000,
		RunGID:            1000,
		IdentityConfigMap: "exradius-run-identity",
		ServiceAccount:    "exradius-run",
		WorkspaceSize:     resource.MustParse("50Gi"),
		MemoryRequest:     resource.MustParse("1Gi"),
		CPURequest:        resource.MustParse("500m"),
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
	run.Spec.Runtime.Env = []v1.EnvVar{{Name: "ANTHROPIC_API_KEY", SecretKey: "provider.ANTHROPIC_API_KEY"}}
	run.Spec.Workstation.Setup = []string{"uv sync", "make migrate"}
	run.Spec.Workstation.Memory = &memory
	run.Spec.Workstation.Env = []v1.EnvVar{
		{Name: "DJANGO_SETTINGS_MODULE", Value: "saleor.settings"},
		{Name: "DATABASE_URL", SecretKey: "env.DATABASE_URL"},
		{Name: "HOME", Value: "/root"}, // replaced by the operator's HOME
	}
	run.Spec.Repositories = []v1.RepositorySpec{
		{Folder: "saleor", URL: "https://github.com/acme/saleor.git", Branch: "main", Push: true, CredentialKey: "repo.0"},
		{Folder: "Dashboard.UI", URL: "https://github.com/acme/dashboard.git"},
	}
	run.Spec.Git = v1.GitSpec{
		SetupEnv: []v1.EnvVar{{Name: "GIT_CONFIG_COUNT", Value: "1"}, {Name: "GIT_CONFIG_KEY_0", Value: "safe.directory"}, {Name: "GIT_CONFIG_VALUE_0", Value: "*"}},
		Env: []v1.EnvVar{
			{Name: "GIT_AUTHOR_NAME", Value: "Developer"},
			{Name: "EXRADIUS_GIT_CREDENTIAL_0", SecretKey: "push.0"},
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
	checkGolden(t, "task", b.Workspace(run), b.Pod(run))
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
		sc := c.SecurityContext
		if sc == nil || !*sc.ReadOnlyRootFilesystem || *sc.AllowPrivilegeEscalation || !*sc.RunAsNonRoot ||
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
	want := []string{"Prepare workspace", "Clone saleor (main)", "Clone Dashboard.UI (default branch)", "Setup: uv sync", "Setup: make migrate"}
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
