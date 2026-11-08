// Package controller holds the Run controller: a Run becomes a workspace PVC and a pod,
// and the pod's state becomes the Run's status (docs/kubernetes.md §5, §8.3, §8.4).
package controller

import (
	"context"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/events"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
	"github.com/musabeytekin/exradius/operator/internal/status"
)

// GracePeriod is how long a cancelled run's containers get after SIGTERM (§8.4).
const GracePeriod int64 = 30

// LogReader reads the end of a container's log.
type LogReader interface {
	Tail(ctx context.Context, namespace, pod, container string) (string, error)
}

// ClientsetLogs reads logs through the API server (pods/log).
type ClientsetLogs struct{ Clientset kubernetes.Interface }

// Tail implements LogReader.
func (l ClientsetLogs) Tail(ctx context.Context, namespace, pod, container string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	data, err := l.Clientset.CoreV1().Pods(namespace).GetLogs(pod, &corev1.PodLogOptions{
		Container: container, TailLines: ptr.To[int64](400),
	}).DoRaw(ctx)
	if err != nil {
		return "", err
	}
	return status.Tail(string(data)), nil
}

// RunReconciler makes the cluster match each Run.
type RunReconciler struct {
	Client client.Client
	// APIReader reads past the cache, to tell a pod that is gone from one not seen yet.
	APIReader client.Reader
	Scheme    *runtime.Scheme
	Builder   podspec.Builder
	Logs      LogReader
	Recorder  events.EventRecorder
	Now       func() time.Time
}

// +kubebuilder:rbac:groups=exradius.dev,resources=runs,verbs=get;list;watch
// +kubebuilder:rbac:groups=exradius.dev,resources=runs/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups="",resources=pods/log,verbs=get
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims,verbs=get;list;watch;create;delete

// Reconcile implements reconcile.Reconciler.
func (r *RunReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	run := &v1.Run{}
	if err := r.Client.Get(ctx, req.NamespacedName, run); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if run.DeletionTimestamp != nil {
		return ctrl.Result{}, nil // garbage collection removes the pod, the PVC and the Secret
	}

	pods := &corev1.PodList{}
	if err := r.Client.List(ctx, pods, client.InNamespace(run.Namespace), client.MatchingLabels{podspec.LabelRun: run.Name}); err != nil {
		return ctrl.Result{}, err
	}

	if run.Spec.Cancel {
		return r.cancel(ctx, run, pods.Items)
	}

	newGeneration := run.Status.PodName == "" || run.Spec.PodGeneration > run.Status.PodGeneration
	if newGeneration {
		return r.startGeneration(ctx, run, pods.Items)
	}
	if run.Status.Phase.Final() {
		return ctrl.Result{}, nil // final phases are sticky for their generation
	}

	var pod *corev1.Pod
	for i := range pods.Items {
		if pods.Items[i].Name == run.Status.PodName {
			pod = &pods.Items[i]
		}
	}
	if pod == nil {
		lost, err := r.gone(ctx, run.Namespace, run.Status.PodName)
		if err != nil || !lost {
			return ctrl.Result{RequeueAfter: time.Second}, err
		}
		st := *run.Status.DeepCopy()
		st.Phase, st.Reason, st.Message = v1.PhaseFailed, v1.ReasonPodLost, "the run's pod was deleted"
		st.ObservedGeneration = run.Generation
		r.event(run, corev1.EventTypeWarning, v1.ReasonPodLost, "PodLost", st.Message)
		return ctrl.Result{}, r.setStatus(ctx, run, st)
	}

	obs := status.Observe(run, pod, r.now())
	for _, container := range obs.Ended {
		text, err := r.Logs.Tail(ctx, pod.Namespace, pod.Name, container)
		if err != nil {
			logger.V(1).Info("cannot read log", "pod", pod.Name, "container", container, "error", err.Error())
			continue
		}
		if strings.TrimSpace(text) != "" {
			setLog(&obs.Status, container, text)
		}
	}
	status.Explain(&obs.Status, pod)
	if pod.DeletionTimestamp != nil && obs.Status.Phase.Final() && !obs.Fail {
		obs.Status.Reason, obs.Status.Message = v1.ReasonPodLost, "the run's pod was deleted"
		obs.Events = append(obs.Events, status.Event{Type: corev1.EventTypeWarning, Reason: v1.ReasonPodLost, Note: obs.Status.Message})
	}
	if obs.Fail && pod.DeletionTimestamp == nil {
		if err := r.Client.Delete(ctx, pod, client.GracePeriodSeconds(GracePeriod)); client.IgnoreNotFound(err) != nil {
			return ctrl.Result{}, err
		}
	}
	for _, e := range obs.Events {
		r.event(run, e.Type, e.Reason, "Observe", e.Note)
	}
	return ctrl.Result{RequeueAfter: obs.RequeueAfter}, r.setStatus(ctx, run, obs.Status)
}

// startGeneration creates the workspace (once) and the pod of spec.podGeneration, after
// the pods of older generations are gone: two runners never share a workspace.
func (r *RunReconciler) startGeneration(ctx context.Context, run *v1.Run, pods []corev1.Pod) (ctrl.Result, error) {
	name := podspec.PodName(run, run.Spec.PodGeneration)
	older := 0
	for i := range pods {
		p := &pods[i]
		if p.Name == name {
			continue
		}
		older++
		if p.DeletionTimestamp == nil {
			if err := r.Client.Delete(ctx, p, client.GracePeriodSeconds(GracePeriod)); client.IgnoreNotFound(err) != nil {
				return ctrl.Result{}, err
			}
		}
	}
	if older > 0 {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if pvc := r.Builder.Workspace(run); pvc != nil {
		if err := r.Client.Create(ctx, pvc); err != nil && !apierrors.IsAlreadyExists(err) {
			return ctrl.Result{}, fmt.Errorf("create workspace: %w", err)
		}
	}
	pod := r.Builder.Pod(run)
	if err := r.Client.Create(ctx, pod); err != nil && !apierrors.IsAlreadyExists(err) {
		return ctrl.Result{}, fmt.Errorf("create pod: %w", err)
	}
	r.event(run, corev1.EventTypeNormal, "PodCreated", "CreatePod", "created pod "+pod.Name)
	return ctrl.Result{}, r.setStatus(ctx, run, status.Reset(run, pod.Name))
}

// cancel deletes the Run's pods and creates none; once none is left the Run is Failed
// with reason Cancelled (a final phase reached before stays).
func (r *RunReconciler) cancel(ctx context.Context, run *v1.Run, pods []corev1.Pod) (ctrl.Result, error) {
	for i := range pods {
		if pods[i].DeletionTimestamp == nil {
			if err := r.Client.Delete(ctx, &pods[i], client.GracePeriodSeconds(GracePeriod)); client.IgnoreNotFound(err) != nil {
				return ctrl.Result{}, err
			}
		}
	}
	if len(pods) > 0 {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	left := &corev1.PodList{}
	if err := r.APIReader.List(ctx, left, client.InNamespace(run.Namespace), client.MatchingLabels{podspec.LabelRun: run.Name}); err != nil {
		return ctrl.Result{}, err
	}
	if len(left.Items) > 0 {
		return ctrl.Result{RequeueAfter: time.Second}, nil
	}
	if run.Status.Phase.Final() {
		return ctrl.Result{}, nil
	}
	st := *run.Status.DeepCopy()
	st.Phase, st.Reason, st.Message = v1.PhaseFailed, v1.ReasonCancelled, "cancelled"
	st.ObservedGeneration = run.Generation
	r.event(run, corev1.EventTypeNormal, v1.ReasonCancelled, "Cancel", "the run's pod is gone")
	return ctrl.Result{}, r.setStatus(ctx, run, st)
}

// gone confirms with an uncached read that a pod no longer exists.
func (r *RunReconciler) gone(ctx context.Context, namespace, name string) (bool, error) {
	err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &corev1.Pod{})
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	return false, err
}

func (r *RunReconciler) setStatus(ctx context.Context, run *v1.Run, st v1.RunStatus) error {
	if equality.Semantic.DeepEqual(run.Status, st) {
		return nil
	}
	run.Status = st
	if err := r.Client.Status().Update(ctx, run); err != nil {
		if apierrors.IsConflict(err) || apierrors.IsNotFound(err) {
			return nil // the next event brings the new version
		}
		return err
	}
	return nil
}

func (r *RunReconciler) event(run *v1.Run, eventType, reason, action, note string) {
	if r.Recorder == nil {
		return
	}
	if len(note) > 1000 {
		note = note[:1000]
	}
	r.Recorder.Eventf(run, nil, eventType, reason, action, "%s", note)
}

func (r *RunReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func setLog(st *v1.RunStatus, container, text string) {
	if container == podspec.ContainerWorkstation && st.Workstation != nil {
		st.Workstation.Log = text
		return
	}
	for i := range st.Steps {
		if st.Steps[i].Container == container {
			st.Steps[i].Log = text
		}
	}
}

// SetupWithManager registers the controller: Runs, and the pods and PVCs they own.
func (r *RunReconciler) SetupWithManager(mgr ctrl.Manager, workers int) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1.Run{}).
		Owns(&corev1.Pod{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		WithOptions(controller.Options{MaxConcurrentReconciles: workers}).
		Named("run").
		Complete(r)
}
