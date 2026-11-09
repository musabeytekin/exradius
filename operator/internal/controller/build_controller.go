package controller

import (
	"context"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/events"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
	"github.com/musabeytekin/exradius/operator/internal/status"
)

// BuildLogEvery is how often the log of a running build is copied onto its status.
const BuildLogEvery = 5 * time.Second

// BuildReconciler runs each Build in a single-use build pod and records its outcome
// (docs/kubernetes.md §12). A final Build keeps its status; its pod is deleted once the
// outcome and the log are recorded, so a succeeded Build costs nothing but the object.
type BuildReconciler struct {
	Client client.Client
	// APIReader reads past the cache, to tell a pod that is gone from one not seen yet.
	APIReader client.Reader
	Builder   podspec.BuildConfig
	Logs      LogReader
	Recorder  events.EventRecorder
	Now       func() time.Time
}

// +kubebuilder:rbac:groups=exradius.dev,resources=builds,verbs=get;list;watch
// +kubebuilder:rbac:groups=exradius.dev,resources=builds/status,verbs=get;update;patch

// Reconcile implements reconcile.Reconciler.
func (r *BuildReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	build := &v1.Build{}
	if err := r.Client.Get(ctx, req.NamespacedName, build); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if build.DeletionTimestamp != nil {
		return ctrl.Result{}, nil // garbage collection removes the pod and the Secret
	}

	name := podspec.BuildPodName(build)
	pod := &corev1.Pod{}
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: build.Namespace, Name: name}, pod)
	if err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	exists := err == nil
	if exists && !metav1.IsControlledBy(pod, build) {
		// The pod of an earlier Build of this name (a failed Build the worker replaced):
		// wait until it is gone.
		if err := r.deletePod(ctx, pod, true); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}

	if build.Status.Phase.Final() {
		return ctrl.Result{}, r.deletePod(ctx, pod, exists) // single-use
	}

	if build.Status.PodName == "" {
		if !exists {
			pod = r.Builder.BuildPod(build)
			if err := r.Client.Create(ctx, pod); err != nil && !apierrors.IsAlreadyExists(err) {
				return ctrl.Result{}, fmt.Errorf("create build pod: %w", err)
			}
			r.event(build, corev1.EventTypeNormal, "PodCreated", "CreatePod", "created build pod "+name)
		}
		st := v1.BuildStatus{Phase: v1.PhasePending, PodName: name, ObservedGeneration: build.Generation}
		_, err := r.setStatus(ctx, build, st)
		return ctrl.Result{RequeueAfter: BuildLogEvery}, err
	}

	if !exists {
		lost, err := r.gone(ctx, build.Namespace, name)
		if err != nil || !lost {
			return ctrl.Result{RequeueAfter: time.Second}, err
		}
		st := *build.Status.DeepCopy()
		st.Phase, st.Reason, st.Message = v1.PhaseFailed, v1.ReasonPodLost, "the build pod was deleted"
		st.FinishedAt = &metav1.Time{Time: r.now()}
		st.ObservedGeneration = build.Generation
		r.event(build, corev1.EventTypeWarning, v1.ReasonPodLost, "PodLost", st.Message)
		_, err = r.setStatus(ctx, build, st)
		return ctrl.Result{}, err
	}

	obs := status.ObserveBuild(build, pod, r.now())
	if obs.LogFrom != "" {
		text, err := r.Logs.Tail(ctx, pod.Namespace, pod.Name, obs.LogFrom)
		if err != nil {
			logger.V(1).Info("cannot read log", "pod", pod.Name, "container", obs.LogFrom, "error", err.Error())
		} else if strings.TrimSpace(text) != "" {
			obs.Status.Log = text
		}
	}
	if pod.DeletionTimestamp != nil && !obs.Status.Phase.Final() {
		// Deleted by someone else while building: the pod will not finish.
		obs.Status.Phase, obs.Status.Reason, obs.Status.Message = v1.PhaseFailed, v1.ReasonPodLost, "the build pod was deleted"
		obs.Status.FinishedAt = &metav1.Time{Time: r.now()}
		obs.Events = append(obs.Events, status.Event{Type: corev1.EventTypeWarning, Reason: v1.ReasonPodLost, Note: obs.Status.Message})
	}
	for _, e := range obs.Events {
		r.event(build, e.Type, e.Reason, "Observe", e.Note)
	}
	stored, err := r.setStatus(ctx, build, obs.Status)
	if err != nil || !stored {
		return ctrl.Result{RequeueAfter: time.Second}, err
	}
	if obs.Status.Phase.Final() {
		// Recorded (with its log): the pod has done its job.
		return ctrl.Result{}, r.deletePod(ctx, pod, true)
	}
	requeue := BuildLogEvery
	if obs.RequeueAfter > 0 && obs.RequeueAfter < requeue {
		requeue = obs.RequeueAfter
	}
	return ctrl.Result{RequeueAfter: requeue}, nil
}

func (r *BuildReconciler) deletePod(ctx context.Context, pod *corev1.Pod, exists bool) error {
	if !exists || pod.DeletionTimestamp != nil {
		return nil
	}
	return client.IgnoreNotFound(r.Client.Delete(ctx, pod, client.GracePeriodSeconds(GracePeriod)))
}

// gone confirms with an uncached read that a pod no longer exists.
func (r *BuildReconciler) gone(ctx context.Context, namespace, name string) (bool, error) {
	err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &corev1.Pod{})
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	return false, err
}

// setStatus writes st unless it is what the Build has; stored reports whether the Build
// now has it (false on a conflict: the next event brings the new version).
func (r *BuildReconciler) setStatus(ctx context.Context, build *v1.Build, st v1.BuildStatus) (bool, error) {
	if equality.Semantic.DeepEqual(build.Status, st) {
		return true, nil
	}
	build.Status = st
	if err := r.Client.Status().Update(ctx, build); err != nil {
		if apierrors.IsConflict(err) || apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func (r *BuildReconciler) event(build *v1.Build, eventType, reason, action, note string) {
	if r.Recorder == nil {
		return
	}
	if len(note) > 1000 {
		note = note[:1000]
	}
	r.Recorder.Eventf(build, nil, eventType, reason, action, "%s", note)
}

func (r *BuildReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// SetupWithManager registers the controller: Builds, and the pods they own. Status-only
// changes of a Build do not reconcile it again (its log changes while it runs); the
// controller requeues itself every BuildLogEvery while a build runs.
func (r *BuildReconciler) SetupWithManager(mgr ctrl.Manager, workers int) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1.Build{}, builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Owns(&corev1.Pod{}).
		WithOptions(controller.Options{MaxConcurrentReconciles: workers}).
		Named("build").
		Complete(r)
}
