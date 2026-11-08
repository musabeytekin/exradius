// Package v1alpha1 holds the exradius.dev/v1alpha1 API: the Run resource.
//
// +kubebuilder:object:generate=true
// +groupName=exradius.dev
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// GroupVersion is the API group and version of the Run resource.
	GroupVersion = schema.GroupVersion{Group: "exradius.dev", Version: "v1alpha1"}

	// SchemeBuilder registers the types of this package.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the types of this package to a scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
