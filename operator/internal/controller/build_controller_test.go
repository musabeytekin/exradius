package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

func newBuild(ns, name string) *v1.Build {
	return &v1.Build{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: v1.BuildSpec{
			ID: "id-" + name, Project: "billing", Name: "workstation",
			Image: "registry:5000/exradius/billing/workstation:1a2b", Cache: "registry:5000/exradius/billing/workstation:buildcache",
			Dockerfile: "Dockerfile", SecretName: name,
		},
	}
}

func getBuild(t *testing.T, ns, name string) *v1.Build {
	t.Helper()
	b := &v1.Build{}
	must(t, k8s.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, b))
	return b
}

func waitBuildPhase(t *testing.T, ns, name string, phase v1.Phase) *v1.Build {
	t.Helper()
	var b *v1.Build
	eventually(t, name+" to be "+string(phase), func() bool {
		b = getBuild(t, ns, name)
		return b.Status.Phase == phase
	})
	return b
}

func waitPodGone(t *testing.T, ns, name string) {
	t.Helper()
	eventually(t, "pod "+name+" to be deleted", func() bool {
		pod, err := getPod(ns, name)
		return apierrors.IsNotFound(err) || (err == nil && pod.DeletionTimestamp != nil)
	})
}

func TestBuildBecomesPodAndSucceeds(t *testing.T) {
	ns := namespace(t)
	must(t, k8s.Create(testCtx, newBuild(ns, "build-billing-workstation-1a2b")))
	name := "build-billing-workstation-1a2b"
	pod := waitPod(t, ns, name)
	created := getBuild(t, ns, name)
	if refs := pod.OwnerReferences; len(refs) != 1 || refs[0].UID != created.UID || !*refs[0].Controller {
		t.Fatalf("the pod is not controlled by the Build: %+v", refs)
	}
	if pod.Labels[podspec.LabelPurpose] != "build" || pod.Spec.InitContainers[0].Name != podspec.ContainerContext {
		t.Fatalf("pod %+v", pod.ObjectMeta)
	}
	if st := waitBuildPhase(t, ns, name, v1.PhasePending).Status; st.PodName != name {
		t.Fatalf("status %+v", st)
	}

	setPodStatus(t, ns, name, func(s *corev1.PodStatus) {
		s.Phase = corev1.PodRunning
		scheduled(s)
		s.StartTime = ptrTime(at())
		s.InitContainerStatuses = []corev1.ContainerStatus{done(podspec.ContainerContext, 0)}
		s.ContainerStatuses = []corev1.ContainerStatus{up(podspec.ContainerBuild)}
	})
	running := waitBuildPhase(t, ns, name, v1.PhaseRunning)
	eventually(t, "the running build's log", func() bool {
		running = getBuild(t, ns, name)
		return running.Status.Log == "log of build"
	})
	if running.Status.StartedAt == nil {
		t.Fatal("startedAt")
	}

	finished := done(podspec.ContainerBuild, 0)
	finished.State.Terminated.Message = `{"containerimage.digest": "sha256:feed"}`
	setPodStatus(t, ns, name, func(s *corev1.PodStatus) {
		s.Phase = corev1.PodSucceeded
		s.ContainerStatuses = []corev1.ContainerStatus{finished}
	})
	b := waitBuildPhase(t, ns, name, v1.PhaseSucceeded)
	if b.Status.Digest != "sha256:feed" || b.Status.FinishedAt == nil {
		t.Fatalf("status %+v", b.Status)
	}
	waitPodGone(t, ns, name) // single-use: deleted once recorded
	if b = getBuild(t, ns, name); b.Status.Phase != v1.PhaseSucceeded {
		t.Fatal("a succeeded Build stays succeeded without its pod")
	}
}

func TestBuildFailsWithItsLog(t *testing.T) {
	ns := namespace(t)
	name := "build-billing-workstation-dead"
	must(t, k8s.Create(testCtx, newBuild(ns, name)))
	waitPod(t, ns, name)
	setPodStatus(t, ns, name, func(s *corev1.PodStatus) {
		s.Phase = corev1.PodFailed
		scheduled(s)
		s.InitContainerStatuses = []corev1.ContainerStatus{done(podspec.ContainerContext, 0)}
		s.ContainerStatuses = []corev1.ContainerStatus{done(podspec.ContainerBuild, 1)}
	})
	b := waitBuildPhase(t, ns, name, v1.PhaseFailed)
	if b.Status.Reason != v1.ReasonBuildFailed || b.Status.Log != "log of build" || b.Status.Message != "the build exited with 1" {
		t.Fatalf("status %+v", b.Status)
	}
	waitPodGone(t, ns, name)
}

func TestBuildContextFetchFails(t *testing.T) {
	ns := namespace(t)
	name := "build-billing-workstation-ctx"
	must(t, k8s.Create(testCtx, newBuild(ns, name)))
	waitPod(t, ns, name)
	setPodStatus(t, ns, name, func(s *corev1.PodStatus) {
		s.Phase = corev1.PodFailed
		scheduled(s)
		s.InitContainerStatuses = []corev1.ContainerStatus{done(podspec.ContainerContext, 1)}
	})
	b := waitBuildPhase(t, ns, name, v1.PhaseFailed)
	if b.Status.Reason != v1.ReasonContextFailed || b.Status.Log != "log of context" {
		t.Fatalf("status %+v", b.Status)
	}
}

func TestBuildPodDeletedByHand(t *testing.T) {
	ns := namespace(t)
	name := "build-billing-workstation-lost"
	must(t, k8s.Create(testCtx, newBuild(ns, name)))
	pod := waitPod(t, ns, name)
	waitBuildPhase(t, ns, name, v1.PhasePending)
	must(t, k8s.Delete(testCtx, pod, client.GracePeriodSeconds(0)))
	b := waitBuildPhase(t, ns, name, v1.PhaseFailed)
	if b.Status.Reason != v1.ReasonPodLost {
		t.Fatalf("status %+v", b.Status)
	}
	if _, err := getPod(ns, name); !apierrors.IsNotFound(err) {
		t.Fatal("no new pod for a final Build")
	}
}

func ptrTime(t metav1.Time) *metav1.Time { return &t }
