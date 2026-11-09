// Package status maps a Run's pod onto the Run's status (docs/kubernetes.md §5): the
// phase, one step per init container, the workstation container, and the failures the
// operator decides itself (image pulls, container config errors).
package status

import (
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

const (
	// LogTail is how much of a container's log a step keeps.
	LogTail = 4000
	// PullAttempts is how many failed pulls of one image fail the Run.
	PullAttempts = 3
	// ConfigErrorGrace is how long a container may wait with a config error (a Secret
	// that is not there yet) before the Run fails.
	ConfigErrorGrace = 120 * time.Second
	// ConditionScheduled mirrors the pod's PodScheduled condition.
	ConditionScheduled = "Scheduled"
)

// Event is a Kubernetes Event to record on the Run.
type Event struct {
	Type   string
	Reason string
	Note   string
}

// Observation is the Run status for the observed pod, and what the controller must do.
type Observation struct {
	Status v1.RunStatus
	// Ended are containers that terminated since the previous status: read their logs.
	Ended []string
	// Fail is set when the operator fails the Run itself: delete the pod.
	Fail bool
	// Events to record.
	Events []Event
	// RequeueAfter asks for another look (a config error's grace period).
	RequeueAfter time.Duration
}

// Tail keeps the last LogTail characters of text.
func Tail(text string) string {
	text = strings.TrimSpace(text)
	runes := []rune(text)
	if len(runes) <= LogTail {
		return text
	}
	return "…" + string(runes[len(runes)-LogTail:])
}

func byName(statuses []corev1.ContainerStatus) map[string]corev1.ContainerStatus {
	out := make(map[string]corev1.ContainerStatus, len(statuses))
	for _, s := range statuses {
		out[s.Name] = s
	}
	return out
}

func timePtr(t metav1.Time) *metav1.Time {
	if t.IsZero() {
		return nil
	}
	return &t
}

func terminal(state v1.StepState) bool { return state == v1.StepPassed || state == v1.StepFailed }

// Reset is the status of a new pod generation: the steps of the Run, nothing observed.
func Reset(run *v1.Run, podName string) v1.RunStatus {
	return v1.RunStatus{
		Phase:              v1.PhasePending,
		PodName:            podName,
		PodGeneration:      run.Spec.PodGeneration,
		Steps:              podspec.StepNames(run),
		ObservedGeneration: run.Generation,
	}
}

// Observe maps the pod of the Run's current generation onto the Run's status. The
// previous status supplies the logs already read and the counters.
func Observe(run *v1.Run, pod *corev1.Pod, now time.Time) Observation {
	prev := run.Status
	st := *prev.DeepCopy()
	st.PodName = pod.Name
	st.NodeName = pod.Spec.NodeName
	st.ObservedGeneration = run.Generation
	obs := Observation{}

	// Steps, keeping what was read before.
	previous := map[string]v1.Step{}
	for _, s := range prev.Steps {
		previous[s.Container] = s
	}
	inits := byName(pod.Status.InitContainerStatuses)
	st.Steps = podspec.StepNames(run)
	for i := range st.Steps {
		step := &st.Steps[i]
		before := previous[step.Container]
		step.Log = before.Log
		cs, ok := inits[step.Container]
		if !ok {
			continue
		}
		switch {
		case cs.State.Terminated != nil:
			t := cs.State.Terminated
			step.StartedAt, step.FinishedAt = timePtr(t.StartedAt), timePtr(t.FinishedAt)
			step.ExitCode = &t.ExitCode
			step.Reason = t.Reason
			if step.Log == "" && t.Message != "" {
				// A container that could not start has no log; the runtime's message is all
				// there is. A log read later replaces it.
				step.Log = Tail(t.Message)
			}
			if t.ExitCode == 0 {
				step.State = v1.StepPassed
			} else {
				step.State = v1.StepFailed
			}
		case cs.State.Running != nil:
			step.State = v1.StepRunning
			step.StartedAt = timePtr(cs.State.Running.StartedAt)
		}
		if terminal(step.State) && !terminal(before.State) {
			obs.Ended = append(obs.Ended, step.Container)
			if step.State == v1.StepFailed {
				obs.Events = append(obs.Events, Event{corev1.EventTypeWarning, v1.ReasonStepFailed,
					fmt.Sprintf("%s exited with %d", step.Name, *step.ExitCode)})
			}
		}
	}

	// The workstation container.
	mains := byName(pod.Status.ContainerStatuses)
	if cs, ok := mains[podspec.ContainerWorkstation]; ok {
		ws := &v1.ContainerState{State: "waiting"}
		if prev.Workstation != nil {
			ws.Log = prev.Workstation.Log
		}
		switch {
		case cs.State.Terminated != nil:
			t := cs.State.Terminated
			ws.State, ws.ExitCode, ws.Reason, ws.Message = "terminated", &t.ExitCode, t.Reason, t.Message
			ws.StartedAt, ws.FinishedAt = timePtr(t.StartedAt), timePtr(t.FinishedAt)
			if ws.Log == "" && t.Message != "" {
				ws.Log = Tail(t.Message)
			}
			if prev.Workstation == nil || prev.Workstation.State != "terminated" {
				obs.Ended = append(obs.Ended, podspec.ContainerWorkstation)
				if t.Reason == v1.ReasonOOMKilled {
					obs.Events = append(obs.Events, Event{corev1.EventTypeWarning, v1.ReasonOOMKilled,
						"the workstation exceeded its memory limit"})
				}
			}
		case cs.State.Running != nil:
			ws.State, ws.StartedAt = "running", timePtr(cs.State.Running.StartedAt)
		case cs.State.Waiting != nil:
			ws.Reason, ws.Message = cs.State.Waiting.Reason, cs.State.Waiting.Message
		}
		st.Workstation = ws
	}

	scheduled := observeScheduling(pod, prev.Conditions, &st.Conditions, run.Generation, &obs.Events)
	failReason, failMessage := observeWaiting(pod, &st.PullFailures, &st.ConfigErrorSince, now, &obs.RequeueAfter)

	// The phase.
	st.Reason, st.Message = "", ""
	switch {
	case failReason != "":
		obs.Fail = true
		st.Phase, st.Reason, st.Message = v1.PhaseFailed, failReason, failMessage
		obs.Events = append(obs.Events, Event{corev1.EventTypeWarning, failReason, failMessage})
	case pod.Status.Phase == corev1.PodSucceeded:
		st.Phase = v1.PhaseSucceeded
	case pod.Status.Phase == corev1.PodFailed:
		st.Phase = v1.PhaseFailed
	case !scheduled:
		st.Phase = v1.PhasePending
		if c := meta.FindStatusCondition(st.Conditions, ConditionScheduled); c != nil && c.Status != metav1.ConditionTrue {
			st.Reason, st.Message = c.Reason, c.Message
		}
	case st.Workstation != nil && st.Workstation.State != "waiting":
		st.Phase = v1.PhaseRunning
	default:
		st.Phase = v1.PhasePreparing
	}
	obs.Status = st
	return obs
}

// observeScheduling mirrors the pod's PodScheduled condition into conditions and reports
// whether the pod is on a node; a new or changed refusal is an Unschedulable event.
func observeScheduling(pod *corev1.Pod, prev []metav1.Condition, conditions *[]metav1.Condition, generation int64, events *[]Event) bool {
	scheduled := true
	for _, c := range pod.Status.Conditions {
		if c.Type != corev1.PodScheduled {
			continue
		}
		reason := c.Reason
		if reason == "" {
			reason = "Scheduled"
		}
		before := meta.FindStatusCondition(prev, ConditionScheduled)
		meta.SetStatusCondition(conditions, metav1.Condition{
			Type: ConditionScheduled, Status: metav1.ConditionStatus(c.Status), Reason: reason,
			Message: c.Message, ObservedGeneration: generation,
		})
		if c.Status != corev1.ConditionTrue {
			scheduled = false
			if before == nil || before.Status == metav1.ConditionTrue || before.Message != c.Message {
				*events = append(*events, Event{corev1.EventTypeWarning, v1.ReasonUnschedulable, c.Message})
			}
		}
	}
	if pod.Spec.NodeName == "" && pod.Status.Phase == corev1.PodPending {
		scheduled = false
	}
	return scheduled
}

// observeWaiting looks at waiting containers: image pulls (counted in failures) and config
// errors (timed from configErrorSince). It returns the failure the operator decides
// itself, if any, and asks for a requeue while a config error's grace period runs.
func observeWaiting(pod *corev1.Pod, failures *[]v1.PullFailure, configErrorSince **metav1.Time, now time.Time,
	requeue *time.Duration) (failReason, failMessage string) {
	configError := false
	all := append(append([]corev1.ContainerStatus{}, pod.Status.InitContainerStatuses...), pod.Status.ContainerStatuses...)
	for _, cs := range all {
		w := cs.State.Waiting
		if w == nil {
			markPull(failures, cs.Name, "")
			continue
		}
		switch w.Reason {
		case "ErrImagePull", "ImagePullBackOff":
			pf := markPull(failures, cs.Name, w.Reason)
			pf.Image, pf.Message = cs.Image, w.Message
			if pf.Attempts >= PullAttempts && failReason == "" {
				failReason = v1.ReasonImagePullFailed
				failMessage = fmt.Sprintf("the image %s of %s cannot be pulled after %d attempts: %s",
					cs.Image, cs.Name, pf.Attempts, w.Message)
			}
		case "InvalidImageName", "ErrImageNeverPull":
			if failReason == "" {
				failReason = v1.ReasonImagePullFailed
				failMessage = fmt.Sprintf("the image %s of %s cannot be pulled: %s", cs.Image, cs.Name, w.Message)
			}
		case "CreateContainerConfigError", "CreateContainerError":
			configError = true
			if *configErrorSince == nil {
				*configErrorSince = &metav1.Time{Time: now}
			}
			waited := now.Sub((*configErrorSince).Time)
			if waited >= ConfigErrorGrace && failReason == "" {
				failReason = v1.ReasonConfigError
				failMessage = fmt.Sprintf("%s could not be created for %s: %s", cs.Name, waited.Round(time.Second), w.Message)
			} else if waited < ConfigErrorGrace {
				*requeue = ConfigErrorGrace - waited
			}
		default:
			markPull(failures, cs.Name, w.Reason)
		}
	}
	if !configError {
		*configErrorSince = nil
	}
	return failReason, failMessage
}

// markPull counts an image pull attempt when a container's waiting reason moves to
// ErrImagePull (or straight to ImagePullBackOff, when the error itself was not seen).
func markPull(failures *[]v1.PullFailure, container, reason string) *v1.PullFailure {
	var pf *v1.PullFailure
	for i := range *failures {
		if (*failures)[i].Container == container {
			pf = &(*failures)[i]
		}
	}
	pulling := reason == "ErrImagePull" || reason == "ImagePullBackOff"
	if pf == nil {
		if !pulling {
			return nil
		}
		*failures = append(*failures, v1.PullFailure{Container: container})
		pf = &(*failures)[len(*failures)-1]
	}
	last := pf.LastReason
	if reason == "ErrImagePull" && last != "ErrImagePull" {
		pf.Attempts++
	} else if reason == "ImagePullBackOff" && last != "ErrImagePull" && last != "ImagePullBackOff" {
		pf.Attempts++
	}
	pf.LastReason = reason
	return pf
}

// Explain fills reason and message of a Failed status the pod decided, from the first
// failed step, else the workstation, else the pod's own reason (Evicted, NodeLost, ...).
// Call it after the logs of ended containers are on the status.
func Explain(st *v1.RunStatus, pod *corev1.Pod) {
	if st.Phase != v1.PhaseFailed || st.Reason != "" {
		return
	}
	for _, step := range st.Steps {
		if step.State != v1.StepFailed {
			continue
		}
		st.Reason = v1.ReasonStepFailed
		if step.Reason == v1.ReasonOOMKilled {
			st.Reason = v1.ReasonOOMKilled
		}
		st.Message = exited(step.Name, step.ExitCode, step.Reason, step.Log)
		return
	}
	if ws := st.Workstation; ws != nil && ws.State == "terminated" && ws.ExitCode != nil && *ws.ExitCode != 0 {
		st.Reason = v1.ReasonError
		if ws.Reason == v1.ReasonOOMKilled {
			st.Reason = v1.ReasonOOMKilled
		}
		st.Message = exited("the workstation", ws.ExitCode, ws.Reason, ws.Log)
		return
	}
	if pod != nil && pod.Status.Reason != "" {
		st.Reason, st.Message = pod.Status.Reason, pod.Status.Message
		return
	}
	st.Reason, st.Message = v1.ReasonError, "the run's pod failed"
}

func exited(what string, code *int32, reason, log string) string {
	text := what + " exited"
	if code != nil {
		text += fmt.Sprintf(" with %d", *code)
	}
	if reason != "" && reason != "Error" && reason != "Completed" {
		text += " (" + reason + ")"
	}
	if log != "" {
		text += ":\n" + log
	}
	return text
}
