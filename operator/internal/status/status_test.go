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
