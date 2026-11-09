package status

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

// BuildObservation is the Build status for the observed build pod, and what the
// controller must do.
type BuildObservation struct {
	Status v1.BuildStatus
	// LogFrom is the container whose log belongs on the status now: the context fetch when
	// it failed, else the build once it started.
	LogFrom string
	// Fail is set when the operator fails the Build itself: delete the pod.
	Fail bool
	// Events to record.
	Events []Event
	// RequeueAfter asks for another look (a config error's grace period).
	RequeueAfter time.Duration
}

// BuildDigest reads the image digest from buildctl's metadata, which the build container
// leaves as its termination message.
func BuildDigest(message string) string {
	var metadata map[string]any
	if json.Unmarshal([]byte(strings.TrimSpace(message)), &metadata) != nil {
		return ""
	}
	digest, _ := metadata["containerimage.digest"].(string)
	return digest
}

// ObserveBuild maps a Build's pod onto the Build's status. The previous status supplies the
// log already read and the counters.
func ObserveBuild(build *v1.Build, pod *corev1.Pod, now time.Time) BuildObservation {
	prev := build.Status
	st := *prev.DeepCopy()
	st.PodName = pod.Name
	st.NodeName = pod.Spec.NodeName
	st.ObservedGeneration = build.Generation
	obs := BuildObservation{}

	scheduled := observeScheduling(pod, prev.Conditions, &st.Conditions, build.Generation, &obs.Events)
	failReason, failMessage := observeWaiting(pod, &st.PullFailures, &st.ConfigErrorSince, now, &obs.RequeueAfter)
	if scheduled && st.StartedAt == nil && pod.Status.StartTime != nil {
		st.StartedAt = pod.Status.StartTime.DeepCopy()
	}

	var contextFailed *corev1.ContainerStateTerminated
	for _, cs := range pod.Status.InitContainerStatuses {
		if cs.Name == podspec.ContainerContext && cs.State.Terminated != nil && cs.State.Terminated.ExitCode != 0 {
			contextFailed = cs.State.Terminated
		}
	}
	var built *corev1.ContainerStateTerminated
	buildStarted := false
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.Name != podspec.ContainerBuild {
			continue
		}
		buildStarted = cs.State.Running != nil || cs.State.Terminated != nil
		built = cs.State.Terminated
	}
	switch {
	case contextFailed != nil:
		obs.LogFrom = podspec.ContainerContext
	case buildStarted:
		obs.LogFrom = podspec.ContainerBuild
	}

	st.Reason, st.Message = "", ""
	switch {
	case failReason != "":
		obs.Fail = true
		st.Phase, st.Reason, st.Message = v1.PhaseFailed, failReason, failMessage
	case contextFailed != nil:
		st.Phase, st.Reason = v1.PhaseFailed, v1.ReasonContextFailed
		st.Message = fmt.Sprintf("fetching the build context failed (exit %d)", contextFailed.ExitCode)
		st.FinishedAt = timePtr(contextFailed.FinishedAt)
	case built != nil && built.ExitCode == 0:
		st.Phase, st.Digest = v1.PhaseSucceeded, BuildDigest(built.Message)
		st.FinishedAt = timePtr(built.FinishedAt)
	case built != nil:
		st.Phase, st.Reason = v1.PhaseFailed, v1.ReasonBuildFailed
		if built.Reason == v1.ReasonOOMKilled {
			st.Reason = v1.ReasonOOMKilled
		}
		st.Message = exited("the build", &built.ExitCode, built.Reason, "")
		st.FinishedAt = timePtr(built.FinishedAt)
	case pod.Status.Phase == corev1.PodFailed:
		st.Phase, st.Reason, st.Message = v1.PhaseFailed, pod.Status.Reason, pod.Status.Message
		if st.Reason == "" {
			st.Reason, st.Message = v1.ReasonError, "the build pod failed"
		}
	case pod.Status.Phase == corev1.PodSucceeded:
		st.Phase = v1.PhaseSucceeded
	case !scheduled:
		st.Phase = v1.PhasePending
		if c := meta.FindStatusCondition(st.Conditions, ConditionScheduled); c != nil && c.Status != metav1.ConditionTrue {
			st.Reason, st.Message = c.Reason, c.Message
		}
	default:
		st.Phase = v1.PhaseRunning
	}
	if st.Phase.Final() {
		if st.FinishedAt == nil {
			st.FinishedAt = &metav1.Time{Time: now}
		}
		if !prev.Phase.Final() {
			if st.Phase == v1.PhaseSucceeded {
				obs.Events = append(obs.Events, Event{corev1.EventTypeNormal, "Built", "pushed " + build.Spec.Image + digestNote(st.Digest)})
			} else {
				obs.Events = append(obs.Events, Event{corev1.EventTypeWarning, st.Reason, st.Message})
			}
		}
	}
	obs.Status = st
	return obs
}

func digestNote(digest string) string {
	if digest == "" {
		return ""
	}
	return "@" + digest
}
