package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Reasons on a Build's status.reason, also used as Event reasons.
const (
	ReasonContextFailed = "ContextFailed"
	ReasonBuildFailed   = "BuildFailed"
)

// BuildSpec is one image to build from a context the api serves (docs/kubernetes.md §12).
// The worker writes it; nothing in it can change the build pod's security.
type BuildSpec struct {
	// ID is the image build's id: the internal API path of its context
	// (/internal/builds/{id}/context).
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`
	// +kubebuilder:validation:MinLength=1
	Project string `json:"project"`
	// Name is workstation or the service's name.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// Image is the reference pushed when the build succeeds, with its tag.
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// Cache is the registry reference of the layer cache (exported with mode=max and
	// imported); empty: no cache.
	// +optional
	Cache string `json:"cache,omitempty"`
	// Dockerfile is the Dockerfile's path inside the context.
	// +kubebuilder:validation:MinLength=1
	Dockerfile string `json:"dockerfile"`
	// SecretName is the Secret with the build's token (key token), in the Build's namespace.
	// +kubebuilder:validation:MinLength=1
	SecretName string `json:"secretName"`
}

// BuildStatus is the build pod's outcome, written by the operator only.
type BuildStatus struct {
	// Phase is Pending (not scheduled), Running, Succeeded or Failed. Final phases stay.
	// +optional
	Phase Phase `json:"phase,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	PodName string `json:"podName,omitempty"`
	// +optional
	NodeName string `json:"nodeName,omitempty"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// Digest is the pushed image's manifest digest.
	// +optional
	Digest string `json:"digest,omitempty"`
	// Log is the last 4000 characters of the build's log, refreshed while it runs.
	// +optional
	Log string `json:"log,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	// +optional
	PullFailures []PullFailure `json:"pullFailures,omitempty"`
	// +optional
	ConfigErrorSince *metav1.Time `json:"configErrorSince,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// Build is one image built from a Dockerfile in a config repository by a single-use
// rootless BuildKit pod. Builds are kept: a succeeded Build means the image exists.
//
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:metadata:annotations="helm.sh/resource-policy=keep"
// +kubebuilder:printcolumn:name="Project",type=string,JSONPath=`.spec.project`
// +kubebuilder:printcolumn:name="Name",type=string,JSONPath=`.spec.name`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=`.spec.image`,priority=1
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Build struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec BuildSpec `json:"spec"`
	// +optional
	Status BuildStatus `json:"status,omitempty"`
}

// BuildList is a list of Builds.
//
// +kubebuilder:object:root=true
type BuildList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Build `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Build{}, &BuildList{})
}
