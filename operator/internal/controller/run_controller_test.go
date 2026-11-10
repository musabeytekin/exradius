package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"go.uber.org/zap/zapcore"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

// envtest runs a real kube-apiserver and etcd, and no kubelet, scheduler or garbage
// collector: the tests write pod status themselves. Pods without a node are deleted at
// once, whatever their grace period.

var (
	k8s     client.Client
	testCtx context.Context
	counter atomic.Int64
)

type fakeLogs struct{}

func (fakeLogs) Tail(_ context.Context, _, _, container string, _ bool) (string, error) {
	return "log of " + container, nil
}

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		fmt.Println("skipping envtest: KUBEBUILDER_ASSETS is not set (make -C operator test)")
		os.Exit(0)
	}
	ctrl.SetLogger(zap.New(zap.UseDevMode(true), zap.Level(zapcore.Level(zapLevel()))))
	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "charts", "exradius", "files", "crds")},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := env.Start()
	if err != nil {
		panic(err)
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		panic(err)
	}
	if err := v1.AddToScheme(scheme); err != nil {
		panic(err)
	}
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
	})
	if err != nil {
		panic(err)
	}
	reconciler := &RunReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Scheme:    scheme,
		Builder: podspec.Pods{Config: podspec.Config{
			APIURL: "http://exradius-api-internal.exradius:8001", RunUID: 1000, RunGID: 1000,
			IdentityConfigMap: "exradius-run-identity", ServiceAccount: "exradius-run",
			WorkspaceSize: resource.MustParse("1Gi"), MemoryRequest: resource.MustParse("1Gi"),
			CPURequest:    resource.MustParse("500m"),
			ProjectVolume: podspec.ProjectVolumeConfig{Enabled: true, AccessMode: corev1.ReadWriteOnce, Size: resource.MustParse("5Gi")},
		}},
		Logs:         fakeLogs{},
		Recorder:     mgr.GetEventRecorder("exradius-operator"),
		SandboxGrace: time.Second,
	}
	if err := reconciler.SetupWithManager(mgr, 4); err != nil {
		panic(err)
	}
	builds := &BuildReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Builder: podspec.BuildConfig{
			APIURL: "http://exradius-api-internal.exradius:8001", Image: "buildkit:rootless",
			ServiceAccount: "exradius-build", BuildkitdConfigMap: "exradius-buildkitd",
		},
		Logs:     fakeLogs{},
		Recorder: mgr.GetEventRecorder("exradius-operator"),
	}
	if err := builds.SetupWithManager(mgr, 4); err != nil {
		panic(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	testCtx = ctx
	go func() {
		if err := mgr.Start(ctx); err != nil {
			panic(err)
		}
	}()
	k8s, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(err)
	}
	code := m.Run()
	cancel()
	_ = env.Stop()
	os.Exit(code)
}

func zapLevel() int8 {
	if os.Getenv("OPERATOR_TEST_VERBOSE") != "" {
		return -1
	}
	return 2 // errors only
}

// namespace makes a fresh namespace with the run ServiceAccount.
func namespace(t *testing.T) string {
	t.Helper()
	name := fmt.Sprintf("runs-%d", counter.Add(1))
	must(t, k8s.Create(testCtx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}))
	must(t, k8s.Create(testCtx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "exradius-run", Namespace: name}}))
	must(t, k8s.Create(testCtx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "exradius-build", Namespace: name}}))
	return name
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func newRun(ns, id string) *v1.Run {
	return &v1.Run{
		ObjectMeta: metav1.ObjectMeta{Name: "run-" + id, Namespace: ns},
		Spec: v1.RunSpec{
			Purpose: v1.PurposeTask, ID: id, Project: "billing", TaskID: "T-" + id, SecretName: "run-" + id,
			Runtime:      v1.RuntimeSpec{Name: "claude_code", Image: "bundle:1"},
			Workstation:  v1.WorkstationSpec{Image: "workstation:1", Setup: []string{"make deps"}},
			Repositories: []v1.RepositorySpec{{Folder: "app", URL: "https://example.com/app.git", Branch: "main"}},
		},
	}
}

func eventually(t *testing.T, what string, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if check() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func getRun(t *testing.T, ns, name string) *v1.Run {
	t.Helper()
	run := &v1.Run{}
	must(t, k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, run))
	return run
}

func getPod(ns, name string) (*corev1.Pod, error) {
	pod := &corev1.Pod{}
	err := k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, pod)
	return pod, err
}

func waitPhase(t *testing.T, ns, name string, phase v1.Phase) *v1.Run {
	t.Helper()
	var run *v1.Run
	eventually(t, fmt.Sprintf("%s to be %s", name, phase), func() bool {
		run = getRun(t, ns, name)
		return run.Status.Phase == phase
	})
	return run
}

func waitPod(t *testing.T, ns, name string) *corev1.Pod {
	t.Helper()
	var pod *corev1.Pod
	eventually(t, "pod "+name, func() bool {
		p, err := getPod(ns, name)
		pod = p
		return err == nil
	})
	return pod
}

// bind places a pod on a node, as the scheduler would.
func bind(t *testing.T, ns, name string) {
	t.Helper()
	pod, err := getPod(ns, name)
	must(t, err)
	if pod.Spec.NodeName != "" {
		return
	}
	binding := &corev1.Binding{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Target:     corev1.ObjectReference{Kind: "Node", Name: "node-1"},
	}
	must(t, k8s.SubResource("binding").Create(testCtx, pod, binding))
}

// setPodStatus writes a pod's status as the kubelet would, on a bound pod.
func setPodStatus(t *testing.T, ns, name string, edit func(*corev1.PodStatus)) {
	t.Helper()
	bind(t, ns, name)
	eventually(t, "the pod status update", func() bool {
		pod, err := getPod(ns, name)
		if err != nil {
			return false
		}
		edit(&pod.Status)
		return k8s.Status().Update(testCtx, pod) == nil
	})
}

func at() metav1.Time { return metav1.NewTime(time.Now().Truncate(time.Second)) }

func done(name string, code int32) corev1.ContainerStatus {
	reason := "Completed"
	if code != 0 {
		reason = "Error"
	}
	return corev1.ContainerStatus{Name: name, Image: "workstation:1", State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: code, Reason: reason, StartedAt: at(), FinishedAt: at(),
	}}}
}

func up(name string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, Image: "workstation:1", State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: at()}}}
}

func held(name, reason, message string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, Image: "workstation:1", State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason, Message: message}}}
}

func scheduled(s *corev1.PodStatus) {
	s.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue, LastTransitionTime: at()}}
}

func TestRunBecomesWorkspaceAndPod(t *testing.T) {
	ns := namespace(t)
	run := newRun(ns, "a1")
	must(t, k8s.Create(testCtx, run))

	pod := waitPod(t, ns, "run-a1-0")
	pvc := &corev1.PersistentVolumeClaim{}
	must(t, k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "run-a1"}, pvc))
	created := getRun(t, ns, "run-a1")
	for _, owned := range []metav1.Object{pod, pvc} {
		refs := owned.GetOwnerReferences()
		if len(refs) != 1 || refs[0].UID != created.UID || refs[0].Controller == nil || !*refs[0].Controller {
			t.Fatalf("%s is not owned by the Run: %+v", owned.GetName(), refs)
		}
	}
	if pod.Spec.Volumes[0].PersistentVolumeClaim.ClaimName != "run-a1" {
		t.Fatal("the pod does not mount the workspace")
	}
	st := waitPhase(t, ns, "run-a1", v1.PhasePending).Status
	if st.PodName != "run-a1-0" || len(st.Steps) != 3 || st.Steps[1].Name != "Clone app (main)" {
		t.Fatalf("status %+v", st)
	}
	if len(created.Finalizers) != 0 {
		t.Fatalf("a Run has no finalizer: %v", created.Finalizers)
	}
}

func TestStatusFollowsThePod(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "b2")))
	waitPod(t, ns, "run-b2-0")

	setPodStatus(t, ns, "run-b2-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodPending
		scheduled(s)
		s.InitContainerStatuses = []corev1.ContainerStatus{done("prepare", 0), up("clone-0")}
	})
	run := waitPhase(t, ns, "run-b2", v1.PhasePreparing)
	eventually(t, "the prepare log", func() bool {
		run = getRun(t, ns, "run-b2")
		return run.Status.Steps[0].State == v1.StepPassed && run.Status.Steps[0].Log == "log of prepare"
	})
	if run.Status.Steps[1].State != v1.StepRunning {
		t.Fatalf("clone step %+v", run.Status.Steps[1])
	}

	inits := []corev1.ContainerStatus{done("prepare", 0), done("clone-0", 0), done("setup-0", 0), up("files")}
	setPodStatus(t, ns, "run-b2-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodRunning
		s.InitContainerStatuses = inits
		s.ContainerStatuses = []corev1.ContainerStatus{up("workstation")}
	})
	waitPhase(t, ns, "run-b2", v1.PhaseRunning)

	setPodStatus(t, ns, "run-b2-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodSucceeded
		s.InitContainerStatuses = []corev1.ContainerStatus{done("prepare", 0), done("clone-0", 0), done("setup-0", 0), done("files", 0)}
		s.ContainerStatuses = []corev1.ContainerStatus{done("workstation", 0)}
	})
	run = waitPhase(t, ns, "run-b2", v1.PhaseSucceeded)
	eventually(t, "the workstation log", func() bool {
		run = getRun(t, ns, "run-b2")
		return run.Status.Workstation != nil && run.Status.Workstation.Log == "log of workstation"
	})
}

func TestFailedStepFailsTheRun(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "c3")))
	waitPod(t, ns, "run-c3-0")
	setPodStatus(t, ns, "run-c3-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodFailed
		scheduled(s)
		s.InitContainerStatuses = []corev1.ContainerStatus{done("prepare", 0), done("clone-0", 0), done("setup-0", 2)}
	})
	run := waitPhase(t, ns, "run-c3", v1.PhaseFailed)
	if run.Status.Reason != v1.ReasonStepFailed || run.Status.Message != "Setup: make deps exited with 2:\nlog of setup-0" {
		t.Fatalf("reason %q message %q", run.Status.Reason, run.Status.Message)
	}
}

func TestCancelDeletesThePod(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "d4")))
	waitPod(t, ns, "run-d4-0")
	run := getRun(t, ns, "run-d4")
	patch := client.MergeFrom(run.DeepCopy())
	run.Spec.Cancel = true
	must(t, k8s.Patch(testCtx, run, patch))

	run = waitPhase(t, ns, "run-d4", v1.PhaseFailed)
	if run.Status.Reason != v1.ReasonCancelled {
		t.Fatalf("reason %q", run.Status.Reason)
	}
	if _, err := getPod(ns, "run-d4-0"); !apierrors.IsNotFound(err) {
		t.Fatalf("the pod is still there: %v", err)
	}
	time.Sleep(500 * time.Millisecond)
	if _, err := getPod(ns, "run-d4-0"); !apierrors.IsNotFound(err) {
		t.Fatal("a cancelled Run got a new pod")
	}
}

func TestPodGenerationRecreatesThePod(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "e5")))
	waitPod(t, ns, "run-e5-0")
	waitPhase(t, ns, "run-e5", v1.PhasePending)
	// The pod is lost while the run waits for an answer.
	old, _ := getPod(ns, "run-e5-0")
	must(t, k8s.Delete(testCtx, old))
	waitPhase(t, ns, "run-e5", v1.PhaseFailed)

	run := getRun(t, ns, "run-e5")
	patch := client.MergeFrom(run.DeepCopy())
	run.Spec.PodGeneration = 1
	must(t, k8s.Patch(testCtx, run, patch))
	pod := waitPod(t, ns, "run-e5-1")
	if pod.Spec.Volumes[0].PersistentVolumeClaim.ClaimName != "run-e5" {
		t.Fatal("the new pod does not mount the same workspace")
	}
	if pod.Labels[podspec.LabelPodGeneration] != "1" {
		t.Fatalf("labels %v", pod.Labels)
	}
	st := waitPhase(t, ns, "run-e5", v1.PhasePending).Status
	if st.PodGeneration != 1 || st.PodName != "run-e5-1" || st.Reason != "" || st.Steps[0].State != v1.StepPending {
		t.Fatalf("status %+v", st)
	}
}

func TestPodDeletedByHandIsLost(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "f6")))
	pod := waitPod(t, ns, "run-f6-0")
	waitPhase(t, ns, "run-f6", v1.PhasePending)
	must(t, k8s.Delete(testCtx, pod))
	run := waitPhase(t, ns, "run-f6", v1.PhaseFailed)
	if run.Status.Reason != v1.ReasonPodLost {
		t.Fatalf("reason %q", run.Status.Reason)
	}
	time.Sleep(500 * time.Millisecond)
	if _, err := getPod(ns, "run-f6-0"); !apierrors.IsNotFound(err) {
		t.Fatal("a lost pod is recreated only when podGeneration is bumped")
	}
}

func TestImagePullFailureFailsTheRun(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "g7")))
	waitPod(t, ns, "run-g7-0")
	for _, reason := range []string{"ErrImagePull", "ImagePullBackOff", "ErrImagePull", "ImagePullBackOff", "ErrImagePull"} {
		setPodStatus(t, ns, "run-g7-0", func(s *corev1.PodStatus) {
			s.Phase = corev1.PodPending
			scheduled(s)
			s.InitContainerStatuses = []corev1.ContainerStatus{held("prepare", reason, "manifest unknown")}
		})
		eventually(t, "the observation of "+reason, func() bool {
			run := getRun(t, ns, "run-g7")
			for _, pf := range run.Status.PullFailures {
				if pf.LastReason == reason {
					return true
				}
			}
			return run.Status.Phase == v1.PhaseFailed
		})
	}
	run := waitPhase(t, ns, "run-g7", v1.PhaseFailed)
	if run.Status.Reason != v1.ReasonImagePullFailed || !strings.Contains(run.Status.Message, "manifest unknown") {
		t.Fatalf("reason %q message %q", run.Status.Reason, run.Status.Message)
	}
	// A bound pod waits for its kubelet, which envtest does not have.
	eventually(t, "the pod's deletion", func() bool {
		pod, err := getPod(ns, "run-g7-0")
		return apierrors.IsNotFound(err) || (err == nil && pod.DeletionTimestamp != nil)
	})
}

// A node that cannot create the pod's sandbox (gVisor and a user namespace, a RuntimeClass
// without its runtime) says so only in Events; the Run fails with the kubelet's message
// instead of waiting forever.
func TestSandboxThatCannotStartFailsTheRun(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newRun(ns, "s1")))
	pod := waitPod(t, ns, "run-s1-0")
	must(t, k8s.Create(testCtx, &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{Name: "run-s1-0.sandbox", Namespace: ns},
		InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: ns, Name: pod.Name, UID: pod.UID},
		Reason:         "FailedCreatePodSandBox", Type: corev1.EventTypeWarning, Count: 7,
		Message: "(combined from similar events): Failed to create pod sandbox: rpc error: code = Unknown desc = " +
			"failed to start sandbox: cannot create gofer process: gofer: error setting namespace of type user",
		LastTimestamp: metav1.Now(),
	}))
	setPodStatus(t, ns, "run-s1-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodPending
		scheduled(s)
		s.InitContainerStatuses = []corev1.ContainerStatus{held("prepare", "PodInitializing", "")}
	})
	run := waitPhase(t, ns, "run-s1", v1.PhaseFailed)
	if run.Status.Reason != v1.ReasonSandboxFailed ||
		!strings.HasPrefix(run.Status.Message, "the run's pod could not be started on node-1: Failed to create pod sandbox") {
		t.Fatalf("reason %q message %q", run.Status.Reason, run.Status.Message)
	}
}

func TestProbeHasNoWorkspace(t *testing.T) {
	ns := namespace(t)
	run := &v1.Run{
		ObjectMeta: metav1.ObjectMeta{Name: "probe-h8", Namespace: ns},
		Spec: v1.RunSpec{
			Purpose: v1.PurposeProbe, ID: "h8", SecretName: "probe-h8",
			Runtime:     v1.RuntimeSpec{Name: "claude_code", Image: "bundle:1"},
			Workstation: v1.WorkstationSpec{Image: "workstation:1"},
		},
	}
	must(t, k8s.Create(testCtx, run))
	pod := waitPod(t, ns, "probe-h8-0")
	if len(pod.Spec.InitContainers) != 0 || pod.Spec.Containers[0].Command[1] != "--probe" {
		t.Fatalf("probe pod %+v", pod.Spec)
	}
	pvc := &corev1.PersistentVolumeClaim{}
	if err := k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "probe-h8"}, pvc); !apierrors.IsNotFound(err) {
		t.Fatalf("a probe has no PVC: %v", err)
	}
}

func TestRunSpecValidation(t *testing.T) {
	ns := namespace(t)
	run := newRun(ns, "i9")
	run.Spec.Purpose = "build"
	if err := k8s.Create(testCtx, run); err == nil {
		t.Fatal("an unknown purpose is accepted")
	}
	run = newRun(ns, "i9")
	run.Spec.Workstation.Env = []v1.EnvVar{{Name: "X", Value: "1", SecretKey: "env.X"}}
	if err := k8s.Create(testCtx, run); err == nil {
		t.Fatal("value and secretKey together are accepted")
	}
}

func serviceRunSpec(ns, id string) *v1.Run {
	run := newRun(ns, id)
	run.Spec.Project = "svc-" + id
	run.Spec.Services = []v1.ServiceSpec{
		{Name: "db", Image: "postgres:16", Ready: &v1.ReadySpec{Exec: []string{"pg_isready"}},
			Env: []v1.EnvVar{{Name: "POSTGRES_PASSWORD", SecretKey: "service.db.POSTGRES_PASSWORD"}}},
		{Name: "cache", Image: "valkey/valkey:8", Ready: &v1.ReadySpec{TCP: 6379}},
	}
	run.Spec.Workstation.Caches = []v1.CacheSpec{{Path: "/home/runner/.cache/uv", Name: "0-cache_uv"}}
	return run
}

// Services become native sidecars on localhost by name; the project volume is created on
// the project's first Run and owned by no Run.
func TestServicesAndProjectVolume(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, serviceRunSpec(ns, "s1")))
	pod := waitPod(t, ns, "run-s1-0")
	var names []string
	for _, c := range pod.Spec.InitContainers {
		names = append(names, c.Name)
	}
	if strings.Join(names, " ") != "prepare clone-0 service-db service-cache setup-0 files" {
		t.Fatalf("init containers %v", names)
	}
	if len(pod.Spec.HostAliases) != 1 || strings.Join(pod.Spec.HostAliases[0].Hostnames, ",") != "db,cache" {
		t.Fatalf("hostAliases %+v", pod.Spec.HostAliases)
	}
	pvc := &corev1.PersistentVolumeClaim{}
	must(t, k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "exradius-project-svc-s1"}, pvc))
	if len(pvc.OwnerReferences) != 0 || pvc.Labels[podspec.LabelVolume] != "project" {
		t.Fatalf("the project volume belongs to no Run: %+v", pvc.ObjectMeta)
	}
	st := waitPhase(t, ns, "run-s1", v1.PhasePending).Status
	if len(st.Steps) != 5 || st.Steps[2].Name != "Start service db" || st.Steps[3].Container != "service-cache" {
		t.Fatalf("steps %+v", st.Steps)
	}

	// db never becomes ready: two minutes after its container started, the Run fails with
	// the service's log, and the pod goes.
	setPodStatus(t, ns, "run-s1-0", func(s *corev1.PodStatus) {
		s.Phase = corev1.PodPending
		scheduled(s)
		long := metav1.NewTime(time.Now().Add(-3 * time.Minute))
		db := corev1.ContainerStatus{Name: "service-db", Image: "postgres:16", Started: ptrBool(false),
			State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: long}}}
		s.InitContainerStatuses = []corev1.ContainerStatus{done("prepare", 0), done("clone-0", 0), db, held("service-cache", "PodInitializing", "")}
	})
	run := waitPhase(t, ns, "run-s1", v1.PhaseFailed)
	step := run.Status.Steps[2]
	if run.Status.Reason != v1.ReasonServiceFailed || step.State != v1.StepFailed || step.Log != "log of service-db" ||
		!strings.Contains(run.Status.Message, "service db (postgres:16) did not become ready within 120s") {
		t.Fatalf("status %+v", run.Status)
	}
	eventually(t, "the pod's deletion", func() bool {
		pod, err := getPod(ns, "run-s1-0")
		return apierrors.IsNotFound(err) || (err == nil && pod.DeletionTimestamp != nil)
	})
}

// A project volume that is still going (its project was deleted) is waited for, then
// made anew, before the pod is created.
func TestProjectVolumeBeingDeletedIsWaitedFor(t *testing.T) {
	ns := namespace(t)
	old := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: "exradius-project-svc-s2", Namespace: ns, Finalizers: []string{"exradius.dev/test-hold"}},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources:   corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("1Gi")}},
		},
	}
	must(t, k8s.Create(testCtx, old))
	must(t, k8s.Delete(testCtx, old))
	must(t, k8s.Create(testCtx, serviceRunSpec(ns, "s2")))
	time.Sleep(1500 * time.Millisecond)
	if _, err := getPod(ns, "run-s2-0"); !apierrors.IsNotFound(err) {
		t.Fatalf("no pod while the old project volume goes: %v", err)
	}
	eventually(t, "the finalizer removal", func() bool {
		pvc := &corev1.PersistentVolumeClaim{}
		if err := k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: old.Name}, pvc); err != nil {
			return apierrors.IsNotFound(err)
		}
		pvc.Finalizers = nil
		return k8s.Update(testCtx, pvc) == nil
	})
	waitPod(t, ns, "run-s2-0")
	fresh := &corev1.PersistentVolumeClaim{}
	must(t, k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: old.Name}, fresh))
	if fresh.DeletionTimestamp != nil || fresh.UID == old.UID {
		t.Fatalf("a new project volume: %+v", fresh.ObjectMeta)
	}
}

func ptrBool(v bool) *bool { return &v }
