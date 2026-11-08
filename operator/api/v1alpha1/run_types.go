package v1alpha1

import (
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Purpose says what a Run is for.
// +kubebuilder:validation:Enum=task;verify;probe
type Purpose string

const (
	PurposeTask   Purpose = "task"
	PurposeVerify Purpose = "verify"
	PurposeProbe  Purpose = "probe"
)

// Phase is the Run's place in its pod's life.
type Phase string

const (
	PhasePending   Phase = "Pending"
	PhasePreparing Phase = "Preparing"
	PhaseRunning   Phase = "Running"
	PhaseSucceeded Phase = "Succeeded"
	PhaseFailed    Phase = "Failed"
)

// Final reports whether the phase ends the current pod generation.
func (p Phase) Final() bool { return p == PhaseSucceeded || p == PhaseFailed }

// Reasons on status.reason, also used as Event reasons.
const (
	ReasonUnschedulable   = "Unschedulable"
	ReasonCancelled       = "Cancelled"
	ReasonPodLost         = "PodLost"
	ReasonStepFailed      = "StepFailed"
	ReasonImagePullFailed = "ImagePullFailed"
	ReasonConfigError     = "CreateContainerConfigError"
	ReasonOOMKilled       = "OOMKilled"
	ReasonError           = "Error"
)

// EnvVar is one environment variable: a literal value, or a key of the Run's Secret.
// +kubebuilder:validation:XValidation:rule="!(has(self.value) && has(self.secretKey))",message="value and secretKey are exclusive"
type EnvVar struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +optional
	Value string `json:"value,omitempty"`
	// SecretKey names a key of spec.secretName.
	// +optional
	SecretKey string `json:"secretKey,omitempty"`
}

// RuntimeSpec is the agent runtime and its bundle image.
type RuntimeSpec struct {
	// Name of the runtime, for example claude_code.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// Image of the runtime bundle, mounted read-only at /opt/exradius/runtime as an image volume.
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// Env reaches the workstation container only (provider variables).
	// +optional
	Env []EnvVar `json:"env,omitempty"`
}

// WorkstationSpec is the container the agent works in, and its setup commands.
type WorkstationSpec struct {
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// Setup commands, one init container each, in order (/bin/sh -c).
	// +optional
	Setup []string `json:"setup,omitempty"`
	// Memory becomes request and limit of the workstation and setup containers.
	// +optional
	Memory *resource.Quantity `json:"memory,omitempty"`
	// Env is the project's environment, for setup commands and the workstation.
	// +optional
	Env []EnvVar `json:"env,omitempty"`
	// Caches are accepted and not used yet (the project volume comes later).
	// +optional
	Caches []string `json:"caches,omitempty"`
}

// RepositorySpec is one repository cloned into /work/<folder>.
type RepositorySpec struct {
	// +kubebuilder:validation:MinLength=1
	Folder string `json:"folder"`
	// +kubebuilder:validation:MinLength=1
	URL string `json:"url"`
	// Branch to check out; empty is the remote's default branch.
	// +optional
	Branch string `json:"branch,omitempty"`
	// +optional
	Push bool `json:"push,omitempty"`
	// CredentialKey names the Secret key with the clone credential; empty is anonymous.
	// +optional
	CredentialKey string `json:"credentialKey,omitempty"`
}

// GitSpec is git configuration at the command-line level, computed by the worker.
type GitSpec struct {
	// SetupEnv reaches the setup containers.
	// +optional
	SetupEnv []EnvVar `json:"setupEnv,omitempty"`
	// Env reaches the workstation container.
	// +optional
	Env []EnvVar `json:"env,omitempty"`
}

// RunSpec is a normalised, secret-free description of one execution. The worker writes
// it from a project's config; the operator turns it into a pod.
type RunSpec struct {
	Purpose Purpose `json:"purpose"`
	// ID is the run, verification or provider test id: the internal API path.
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`
	// +optional
	Project string `json:"project,omitempty"`
	// +optional
	TaskID string `json:"taskId,omitempty"`
	// SecretName is the per-run Secret, in the Run's namespace.
	// +kubebuilder:validation:MinLength=1
	SecretName string      `json:"secretName"`
	Runtime    RuntimeSpec `json:"runtime"`
	// Model is the probe's model; empty is the runtime's default.
	// +optional
	Model       string          `json:"model,omitempty"`
	Workstation WorkstationSpec `json:"workstation"`
	// +optional
	Repositories []RepositorySpec `json:"repositories,omitempty"`
	// +optional
	Git GitSpec `json:"git,omitempty"`
	// Cancel deletes the pod and creates no other.
	// +optional
	Cancel bool `json:"cancel,omitempty"`
	// PodGeneration is bumped by the worker to recreate a lost pod on the same workspace.
	// +kubebuilder:validation:Minimum=0
	// +optional
	PodGeneration int64 `json:"podGeneration,omitempty"`
}

// StepState is the state of one step.
type StepState string

const (
	StepPending StepState = "pending"
	StepRunning StepState = "running"
	StepPassed  StepState = "passed"
	StepFailed  StepState = "failed"
)

// Step is one init container of the pod.
type Step struct {
	Name      string    `json:"name"`
	Container string    `json:"container"`
	State     StepState `json:"state"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// +optional
	ExitCode *int32 `json:"exitCode,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// Log is the last 4000 characters of the container's log, read when it ended.
	// +optional
	Log string `json:"log,omitempty"`
}

// ContainerState is the workstation container's state.
type ContainerState struct {
	// State is waiting, running or terminated.
	State string `json:"state"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// +optional
	ExitCode *int32 `json:"exitCode,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	Log string `json:"log,omitempty"`
}

// PullFailure counts the failed pulls of one container's image.
type PullFailure struct {
	Container string `json:"container"`
	// +optional
	Image    string `json:"image,omitempty"`
	Attempts int32  `json:"attempts"`
	// LastReason is the waiting reason last seen, so an attempt is counted once.
	// +optional
	LastReason string `json:"lastReason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
}

// RunStatus is the live execution, written by the operator only.
type RunStatus struct {
	// +optional
	Phase Phase `json:"phase,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	PodName string `json:"podName,omitempty"`
	// PodGeneration is the generation of the pod last created.
	// +optional
	PodGeneration int64 `json:"podGeneration,omitempty"`
	// +optional
	NodeName string `json:"nodeName,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	// Steps are the pod's init containers in order, without the files sidecar.
	// +optional
	Steps []Step `json:"steps,omitempty"`
	// +optional
	Workstation *ContainerState `json:"workstation,omitempty"`
	// +optional
	PullFailures []PullFailure `json:"pullFailures,omitempty"`
	// ConfigErrorSince is when a container of the current pod was first seen waiting
	// with CreateContainerConfigError.
	// +optional
	ConfigErrorSince *metav1.Time `json:"configErrorSince,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// Run is one execution of an agent task, a project verification or a provider probe.
//
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:metadata:annotations="helm.sh/resource-policy=keep"
// +kubebuilder:printcolumn:name="Project",type=string,JSONPath=`.spec.project`
// +kubebuilder:printcolumn:name="Task",type=string,JSONPath=`.spec.taskId`
// +kubebuilder:printcolumn:name="Purpose",type=string,JSONPath=`.spec.purpose`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Run struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec RunSpec `json:"spec"`
	// +optional
	Status RunStatus `json:"status,omitempty"`
}

// RunList is a list of Runs.
//
// +kubebuilder:object:root=true
type RunList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Run `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Run{}, &RunList{})
}
