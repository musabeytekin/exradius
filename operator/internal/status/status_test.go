package status

import (
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

var now = time.Date(2026, 11, 8, 12, 0, 0, 0, time.UTC)

func run() *v1.Run {
	r := &v1.Run{
		ObjectMeta: metav1.ObjectMeta{Name: "run-1", Namespace: "exradius-runs", Generation: 2},
		Spec: v1.RunSpec{
			Purpose: v1.PurposeTask, ID: "1", SecretName: "run-1",
			Runtime:      v1.RuntimeSpec{Name: "claude_code", Image: "bundle"},
			Workstation:  v1.WorkstationSpec{Image: "ws", Setup: []string{"make deps"}},
			Repositories: []v1.RepositorySpec{{Folder: "app", URL: "https://example/app.git", Branch: "main"}},
		},
	}
	r.Status = Reset(r, "run-1-0")
	return r
}

func pod(phase corev1.PodPhase, node string, inits []corev1.ContainerStatus, mains []corev1.ContainerStatus) *corev1.Pod {
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "run-1-0"},
		Spec:       corev1.PodSpec{NodeName: node},
		Status:     corev1.PodStatus{Phase: phase, InitContainerStatuses: inits, ContainerStatuses: mains},
	}
	if node != "" {
		p.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}}
	}
	return p
}

func terminated(name string, code int32, reason string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: code, Reason: reason, StartedAt: metav1.NewTime(now), FinishedAt: metav1.NewTime(now),
	}}}
}

func running(name string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(now)}}}
}

func waiting(name, reason, message string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, Image: "registry/" + name + ":1", State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason, Message: message}}}
}

func TestUnscheduled(t *testing.T) {
	p := pod(corev1.PodPending, "", nil, nil)
	p.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionFalse,
		Reason: "Unschedulable", Message: "0/1 nodes are available: 1 Insufficient memory."}}
	obs := Observe(run(), p, now)
	if obs.Status.Phase != v1.PhasePending || obs.Status.Reason != v1.ReasonUnschedulable ||
		!strings.Contains(obs.Status.Message, "Insufficient memory") {
		t.Fatalf("got %+v", obs.Status)
	}
	if len(obs.Events) != 1 || obs.Events[0].Reason != v1.ReasonUnschedulable {
		t.Fatalf("events %+v", obs.Events)
	}
	// The same condition again records nothing new.
	r := run()
	r.Status = obs.Status
	if again := Observe(r, p, now); len(again.Events) != 0 {
		t.Fatalf("repeated event %+v", again.Events)
	}
}

func TestPreparingWithSteps(t *testing.T) {
	p := pod(corev1.PodPending, "node", []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"),
		running(podspec.CloneContainer(0)),
		waiting(podspec.SetupContainer(0), "PodInitializing", ""),
		waiting(podspec.ContainerFiles, "PodInitializing", ""),
	}, []corev1.ContainerStatus{waiting(podspec.ContainerWorkstation, "PodInitializing", "")})
	obs := Observe(run(), p, now)
	st := obs.Status
	if st.Phase != v1.PhasePreparing {
		t.Fatalf("phase %s", st.Phase)
	}
	want := []v1.StepState{v1.StepPassed, v1.StepRunning, v1.StepPending}
	if len(st.Steps) != 3 {
		t.Fatalf("steps %+v", st.Steps)
	}
	for i, s := range st.Steps {
		if s.State != want[i] {
			t.Errorf("step %s: %s, want %s", s.Name, s.State, want[i])
		}
	}
	if st.Steps[1].Name != "Clone app (main)" || st.Steps[2].Name != "Setup: make deps" {
		t.Errorf("names %q %q", st.Steps[1].Name, st.Steps[2].Name)
	}
	if len(obs.Ended) != 1 || obs.Ended[0] != podspec.ContainerPrepare {
		t.Fatalf("ended %v", obs.Ended)
	}
}

func TestRunningAndSucceeded(t *testing.T) {
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"),
		terminated(podspec.CloneContainer(0), 0, "Completed"),
		terminated(podspec.SetupContainer(0), 0, "Completed"),
		running(podspec.ContainerFiles),
	}
	r := run()
	obs := Observe(r, pod(corev1.PodRunning, "node", inits, []corev1.ContainerStatus{running(podspec.ContainerWorkstation)}), now)
	if obs.Status.Phase != v1.PhaseRunning || obs.Status.Workstation.State != "running" {
		t.Fatalf("got %+v", obs.Status)
	}
	if len(obs.Ended) != 3 {
		t.Fatalf("ended %v", obs.Ended)
	}
	r.Status = obs.Status
	done := Observe(r, pod(corev1.PodSucceeded, "node", inits, []corev1.ContainerStatus{terminated(podspec.ContainerWorkstation, 0, "Completed")}), now)
	if done.Status.Phase != v1.PhaseSucceeded || done.Status.Reason != "" {
		t.Fatalf("got %+v", done.Status)
	}
	if len(done.Ended) != 1 || done.Ended[0] != podspec.ContainerWorkstation {
		t.Fatalf("only the workstation ended now: %v", done.Ended)
	}
}

// The Run ends with its workstation, not with the pod: the services' sidecars are stopped
// after it, and one that ignores SIGTERM keeps the pod Running for the grace period.
func TestEndsWithTheWorkstationWhileSidecarsStop(t *testing.T) {
	r := serviceRun()
	db := podspec.ServiceContainer("db")
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"), terminated(podspec.CloneContainer(0), 0, "Completed"),
		started(runningSince(db, now.Add(-time.Minute)), true), started(runningSince(podspec.ServiceContainer("cache"), now.Add(-time.Minute)), true),
		terminated(podspec.SetupContainer(0), 0, "Completed"),
	}
	for code, phase := range map[int32]v1.Phase{0: v1.PhaseSucceeded, 1: v1.PhaseFailed} {
		obs := Observe(r, pod(corev1.PodRunning, "node", inits,
			[]corev1.ContainerStatus{terminated(podspec.ContainerWorkstation, code, "Completed")}), now)
		if obs.Status.Phase != phase || obs.Fail {
			t.Fatalf("exit %d: got %+v", code, obs.Status)
		}
	}
}

func TestFailedStepWithLog(t *testing.T) {
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"),
		terminated(podspec.CloneContainer(0), 0, "Completed"),
		terminated(podspec.SetupContainer(0), 2, "Error"),
	}
	p := pod(corev1.PodFailed, "node", inits, []corev1.ContainerStatus{waiting(podspec.ContainerWorkstation, "PodInitializing", "")})
	obs := Observe(run(), p, now)
	st := obs.Status
	if st.Phase != v1.PhaseFailed || obs.Fail {
		t.Fatalf("got %+v fail=%v", st, obs.Fail)
	}
	for i := range st.Steps {
		if st.Steps[i].Container == podspec.SetupContainer(0) {
			st.Steps[i].Log = Tail("make: *** No rule to make target 'deps'.")
		}
	}
	Explain(&st, p)
	if st.Reason != v1.ReasonStepFailed || st.Message != "Setup: make deps exited with 2:\nmake: *** No rule to make target 'deps'." {
		t.Fatalf("reason %q message %q", st.Reason, st.Message)
	}
	if len(obs.Events) != 1 || obs.Events[0].Reason != v1.ReasonStepFailed {
		t.Fatalf("events %+v", obs.Events)
	}
}

func TestOOMKilledWorkstation(t *testing.T) {
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"),
		terminated(podspec.CloneContainer(0), 0, "Completed"),
		terminated(podspec.SetupContainer(0), 0, "Completed"),
	}
	p := pod(corev1.PodFailed, "node", inits, []corev1.ContainerStatus{terminated(podspec.ContainerWorkstation, 137, "OOMKilled")})
	obs := Observe(run(), p, now)
	st := obs.Status
	st.Workstation.Log = "killed"
	Explain(&st, p)
	if st.Reason != v1.ReasonOOMKilled || st.Message != "the workstation exited with 137 (OOMKilled):\nkilled" {
		t.Fatalf("reason %q message %q", st.Reason, st.Message)
	}
	found := false
	for _, e := range obs.Events {
		found = found || e.Reason == v1.ReasonOOMKilled
	}
	if !found {
		t.Fatalf("no OOMKilled event: %+v", obs.Events)
	}
}

func TestEvictedPod(t *testing.T) {
	p := pod(corev1.PodFailed, "node", nil, []corev1.ContainerStatus{terminated(podspec.ContainerWorkstation, 0, "Completed")})
	p.Status.Reason, p.Status.Message = "Evicted", "The node was low on resource: ephemeral-storage."
	obs := Observe(run(), p, now)
	st := obs.Status
	Explain(&st, p)
	if st.Reason != "Evicted" || !strings.Contains(st.Message, "ephemeral-storage") {
		t.Fatalf("got %q %q", st.Reason, st.Message)
	}
}

func TestImagePullCountedToFailure(t *testing.T) {
	r := run()
	sequence := []string{"ErrImagePull", "ImagePullBackOff", "ErrImagePull", "ImagePullBackOff", "ImagePullBackOff", "ErrImagePull"}
	var obs Observation
	for i, reason := range sequence {
		p := pod(corev1.PodPending, "node", []corev1.ContainerStatus{
			waiting(podspec.ContainerPrepare, reason, "pull access denied, repository does not exist"),
		}, nil)
		obs = Observe(r, p, now)
		r.Status = obs.Status
		if i < len(sequence)-1 && obs.Fail {
			t.Fatalf("failed after %d observations", i+1)
		}
	}
	if !obs.Fail || obs.Status.Phase != v1.PhaseFailed || obs.Status.Reason != v1.ReasonImagePullFailed {
		t.Fatalf("got %+v", obs.Status)
	}
	if !strings.Contains(obs.Status.Message, "registry/prepare:1") || !strings.Contains(obs.Status.Message, "pull access denied") {
		t.Fatalf("message %q", obs.Status.Message)
	}
	if obs.Status.PullFailures[0].Attempts != 3 {
		t.Fatalf("attempts %d", obs.Status.PullFailures[0].Attempts)
	}
}

func TestInvalidImageFailsAtOnce(t *testing.T) {
	p := pod(corev1.PodPending, "node", []corev1.ContainerStatus{waiting(podspec.ContainerPrepare, "InvalidImageName", "bad ref")}, nil)
	if obs := Observe(run(), p, now); !obs.Fail || obs.Status.Reason != v1.ReasonImagePullFailed {
		t.Fatalf("got %+v", obs.Status)
	}
}

func TestCreateContainerConfigErrorGrace(t *testing.T) {
	r := run()
	p := pod(corev1.PodPending, "node", []corev1.ContainerStatus{
		waiting(podspec.ContainerPrepare, "CreateContainerConfigError", `secret "run-1" not found`),
	}, nil)
	first := Observe(r, p, now)
	if first.Fail || first.Status.ConfigErrorSince == nil || first.RequeueAfter != ConfigErrorGrace {
		t.Fatalf("got %+v requeue %s", first.Status, first.RequeueAfter)
	}
	r.Status = first.Status
	if later := Observe(r, p, now.Add(60*time.Second)); later.Fail || later.Status.Phase != v1.PhasePreparing {
		t.Fatalf("failed within the grace period: %+v", later.Status)
	}
	late := Observe(r, p, now.Add(ConfigErrorGrace))
	if !late.Fail || late.Status.Reason != v1.ReasonConfigError || !strings.Contains(late.Status.Message, "not found") {
		t.Fatalf("got %+v", late.Status)
	}
	// The Secret arrived: the clock resets.
	ok := pod(corev1.PodPending, "node", []corev1.ContainerStatus{running(podspec.ContainerPrepare)}, nil)
	if cleared := Observe(r, ok, now.Add(10*time.Second)); cleared.Status.ConfigErrorSince != nil {
		t.Fatal("the config error clock was not cleared")
	}
}

func TestStartErrorKeepsTheRuntimesMessage(t *testing.T) {
	// A container that cannot start has no log: the step shows the runtime's message.
	failed := terminated(podspec.ContainerPrepare, 128, "StartError")
	failed.State.Terminated.Message = "exec: \"/opt/exradius/runtime/bin/exradius-runner\": no such file or directory"
	p := pod(corev1.PodFailed, "node", []corev1.ContainerStatus{failed}, nil)
	obs := Observe(run(), p, now)
	Explain(&obs.Status, p)
	step := obs.Status.Steps[0]
	if step.State != v1.StepFailed || !strings.Contains(step.Log, "no such file or directory") {
		t.Fatalf("step %+v", step)
	}
	if !strings.Contains(obs.Status.Message, "no such file or directory") {
		t.Fatalf("message %q", obs.Status.Message)
	}
}

func TestTail(t *testing.T) {
	long := strings.Repeat("x", LogTail+10) + "end"
	got := Tail(long)
	if !strings.HasPrefix(got, "…") || !strings.HasSuffix(got, "end") || len([]rune(got)) != LogTail+1 {
		t.Fatalf("tail of %d runes", len([]rune(got)))
	}
	if Tail("  short \n") != "short" {
		t.Fatal("short logs are kept, trimmed")
	}
}

func serviceRun() *v1.Run {
	r := run()
	r.Spec.Services = []v1.ServiceSpec{
		{Name: "db", Image: "postgres:16", Ready: &v1.ReadySpec{Exec: []string{"pg_isready", "-U", "app"}}},
		{Name: "cache", Image: "valkey/valkey:8", Ready: &v1.ReadySpec{TCP: 6379}},
	}
	r.Status = Reset(r, "run-1-0")
	return r
}

func started(cs corev1.ContainerStatus, value bool) corev1.ContainerStatus {
	cs.Started = &value
	return cs
}

func runningSince(name string, since time.Time) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(since)}}}
}

func stepOf(st v1.RunStatus, container string) v1.Step {
	for _, s := range st.Steps {
		if s.Container == container {
			return s
		}
	}
	return v1.Step{}
}

// A service's step runs while its startup probe waits, passes once it is started, and
// stays passed when the pod's end stops it.
func TestServiceSteps(t *testing.T) {
	r := serviceRun()
	db, cache := podspec.ServiceContainer("db"), podspec.ServiceContainer("cache")
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"), terminated(podspec.CloneContainer(0), 0, "Completed"),
		started(runningSince(db, now.Add(-10*time.Second)), false), waiting(cache, "PodInitializing", ""),
	}
	obs := Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	if s := stepOf(obs.Status, db); s.Name != "Start service db" || s.State != v1.StepRunning || obs.Fail {
		t.Fatalf("waiting for its probe: %+v", s)
	}
	if obs.RequeueAfter <= 0 || obs.RequeueAfter > ServiceDeadline {
		t.Fatalf("requeued for the deadline: %v", obs.RequeueAfter)
	}
	r.Status = obs.Status

	inits[2] = started(runningSince(db, now.Add(-10*time.Second)), true)
	inits[3] = started(runningSince(cache, now), false)
	obs = Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	if s := stepOf(obs.Status, db); s.State != v1.StepPassed {
		t.Fatalf("started: %+v", s)
	}
	r.Status = obs.Status

	// The workstation ran and ended: every service is stopped with 143, and stays passed.
	inits[2] = terminated(db, 143, "Error")
	inits[3] = terminated(cache, 143, "Error")
	obs = Observe(r, pod(corev1.PodSucceeded, "node", append(inits, terminated(podspec.SetupContainer(0), 0, "Completed")),
		[]corev1.ContainerStatus{terminated(podspec.ContainerWorkstation, 0, "Completed")}), now)
	if obs.Status.Phase != v1.PhaseSucceeded || stepOf(obs.Status, db).State != v1.StepPassed ||
		stepOf(obs.Status, cache).State != v1.StepPassed || obs.Fail {
		t.Fatalf("services stay passed: %+v", obs.Status)
	}
}

func TestServiceDeadline(t *testing.T) {
	r := serviceRun()
	db := podspec.ServiceContainer("db")
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"), terminated(podspec.CloneContainer(0), 0, "Completed"),
		started(runningSince(db, now.Add(-ServiceDeadline)), false),
	}
	obs := Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	s := stepOf(obs.Status, db)
	if !obs.Fail || obs.Status.Phase != v1.PhaseFailed || obs.Status.Reason != v1.ReasonServiceFailed ||
		s.State != v1.StepFailed || s.Message != "did not become ready within 120s waiting for command pg_isready -U app" {
		t.Fatalf("deadline: %+v / %+v", obs.Status, s)
	}
	if obs.Status.Message != "service db (postgres:16) did not become ready within 120s waiting for command pg_isready -U app" {
		t.Fatalf("message %q", obs.Status.Message)
	}
	if !contains(obs.Ended, db) {
		t.Fatalf("its log is read: %+v", obs.Ended)
	}
}

// A service that crashes before it is ready is restarted by Kubernetes; the operator fails
// the Run instead, with the crashed instance's log.
func TestServiceRestartedBeforeReady(t *testing.T) {
	r := serviceRun()
	cache := podspec.ServiceContainer("cache")
	restarted := started(runningSince(cache, now), false)
	restarted.RestartCount = 1
	restarted.LastTerminationState = corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: 137, Reason: "OOMKilled", StartedAt: metav1.NewTime(now.Add(-5 * time.Second)), FinishedAt: metav1.NewTime(now),
	}}
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"), terminated(podspec.CloneContainer(0), 0, "Completed"),
		started(running(podspec.ServiceContainer("db")), true), restarted,
	}
	obs := Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	s := stepOf(obs.Status, cache)
	if !obs.Fail || s.State != v1.StepFailed || s.Reason != "OOMKilled" || *s.ExitCode != 137 ||
		s.Message != "exited with code 137 before it was ready" {
		t.Fatalf("restart: %+v", s)
	}
	if len(obs.Previous) != 1 || obs.Previous[0] != cache || contains(obs.Ended, cache) {
		t.Fatalf("the previous instance's log: %+v / %+v", obs.Previous, obs.Ended)
	}

	// Exited and not restarted yet: its own log.
	r = serviceRun()
	inits[3] = terminated(cache, 1, "Error")
	obs = Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	if s := stepOf(obs.Status, cache); !obs.Fail || s.Message != "exited with code 1 before it was ready" || !contains(obs.Ended, cache) {
		t.Fatalf("exited: %+v %+v", s, obs.Ended)
	}
}

// A later container that started proves the service started, even if no status said so.
func TestServicePassedWhenLaterContainersStarted(t *testing.T) {
	r := serviceRun()
	restarted := started(runningSince(podspec.ServiceContainer("db"), now), false)
	restarted.RestartCount = 2
	restarted.LastTerminationState = corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 1}}
	inits := []corev1.ContainerStatus{
		terminated(podspec.ContainerPrepare, 0, "Completed"), terminated(podspec.CloneContainer(0), 0, "Completed"),
		restarted, started(running(podspec.ServiceContainer("cache")), true), running(podspec.SetupContainer(0)),
	}
	obs := Observe(r, pod(corev1.PodPending, "node", inits, nil), now)
	if obs.Fail || stepOf(obs.Status, podspec.ServiceContainer("db")).State != v1.StepPassed {
		t.Fatalf("a crash after it was started is not a failure: %+v", obs.Status)
	}
}

func contains(list []string, item string) bool {
	for _, x := range list {
		if x == item {
			return true
		}
	}
	return false
}
