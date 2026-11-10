package podspec

import (
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
)

// Build pods (docs/kubernetes.md §12): one single-use rootless BuildKit pod per Build. An
// init container fetches the context tar from the api's internal port with the Build's
// token; the build container runs buildctl-daemonless.sh, pushes the image and the layer
// cache, and writes buildctl's metadata (the digest) as its termination message.

// Container names of a build pod.
const (
	ContainerContext = "context"
	ContainerBuild   = "build"
)

// Paths inside build containers.
const (
	BuildWorkspace = "/workspace"
	BuildContext   = BuildWorkspace + "/context"
	buildkitState  = "/home/user/.local/share/buildkit"
	buildkitRun    = "/run/user/1000"
	buildkitConfig = "/etc/buildkit"
	buildMetadata  = "/tmp/metadata.json"
)

// BuilderUID is the user of the rootless BuildKit image.
const BuilderUID int64 = 1000

// PurposeBuild is the purpose label of build pods.
const PurposeBuild = "build"

// LabelImage names the image a build pod builds (workstation or a service).
const LabelImage = "exradius.dev/image"

// BuildConfig is the operator's part of every build pod, from the chart's values.
type BuildConfig struct {
	APIURL string
	// Image is the rootless BuildKit image (moby/buildkit:<version>-rootless, pinned).
	Image string
	// ServiceAccount of build pods; its token is never mounted.
	ServiceAccount string
	// BuildkitdConfigMap holds buildkitd.toml (registry hosts reached over HTTP); empty: none.
	BuildkitdConfigMap string
	Resources          corev1.ResourceRequirements
	NodeSelector       map[string]string
	Tolerations        []corev1.Toleration
	ImagePullSecrets   []string
	// Proxy is the egress proxy's forward address when builds reach the network through it
	// (values builds.egress: proxy): buildkitd's pulls and pushes and every RUN step use it.
	// Empty: builds reach the network directly.
	Proxy string
	// NoProxy are the hosts a build reaches directly (the api's internal host).
	NoProxy []string
	// SeccompProfile is a Localhost seccomp profile for the build container, relative to
	// the kubelet's seccomp directory (deploy/seccomp/buildkit.json, installed on every
	// node). With it the build pod fits Pod Security baseline; empty: seccomp and AppArmor
	// Unconfined, which needs privileged (docs/kubernetes.md §12).
	SeccompProfile string
	// AppArmorProfile is a Localhost AppArmor profile for the build container with
	// SeccompProfile, on nodes with AppArmor (deploy/apparmor/exradius-buildkit, loaded on
	// every node): the runtime's default profile denies mount, which rootlesskit needs.
	// Empty: the runtime's default (nodes without AppArmor).
	AppArmorProfile string
}

// proxyVars are the proxy variables of a build, upper and lower case.
func (c BuildConfig) proxyVars() [][2]string {
	if c.Proxy == "" {
		return nil
	}
	none := strings.Join(append([]string{"localhost", "127.0.0.1", "::1"}, c.NoProxy...), ",")
	return [][2]string{
		{"HTTP_PROXY", c.Proxy}, {"HTTPS_PROXY", c.Proxy}, {"NO_PROXY", none},
		{"http_proxy", c.Proxy}, {"https_proxy", c.Proxy}, {"no_proxy", none},
	}
}

// contextScript fetches and unpacks the context. The token stays in the environment, out
// of the command line.
const contextScript = `set -eo pipefail
mkdir -p ` + BuildContext + `
wget -q -O - --header "Authorization: Bearer $EXRADIUS_BUILD_TOKEN" \
  "$EXRADIUS_API_URL/internal/builds/$EXRADIUS_BUILD_ID/context" | tar -x -f - -C ` + BuildContext + `
echo "fetched the build context: $(find ` + BuildContext + ` -type f | wc -l) files"`

// buildScript runs the build and copies buildctl's metadata into the termination message
// (buildctl writes its metadata file through a temporary file next to it, which /dev does
// not allow).
const buildScript = `buildctl-daemonless.sh "$@" && cat ` + buildMetadata + ` > /dev/termination-log`

// BuildPodName is the pod of a Build: the Build's own name (one pod per Build).
func BuildPodName(build *v1.Build) string { return build.Name }

// BuildArgs are buildctl's arguments for a Build. With the proxy, RUN steps get its
// variables as BuildKit's predefined proxy build arguments, which stay out of the image and
// its cache keys.
func (c BuildConfig) BuildArgs(build *v1.Build) []string {
	args := []string{
		"build", "--progress", "plain", "--frontend", "dockerfile.v0",
		"--local", "context=" + BuildContext, "--local", "dockerfile=" + BuildContext,
		"--opt", "filename=" + build.Spec.Dockerfile,
		"--output", "type=image,name=" + build.Spec.Image + ",push=true",
	}
	if build.Spec.Cache != "" {
		args = append(args,
			"--export-cache", "type=registry,ref="+build.Spec.Cache+",mode=max",
			"--import-cache", "type=registry,ref="+build.Spec.Cache,
		)
	}
	for _, kv := range c.proxyVars() {
		args = append(args, "--opt", "build-arg:"+kv[0]+"="+kv[1])
	}
	return append(args, "--metadata-file", buildMetadata)
}

// BuildPod is the pod of a Build.
func (c BuildConfig) BuildPod(build *v1.Build) *corev1.Pod {
	labels := map[string]string{LabelPurpose: PurposeBuild, LabelImage: build.Spec.Name}
	if build.Spec.Project != "" {
		labels[LabelProject] = build.Spec.Project
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:            BuildPodName(build),
			Namespace:       build.Namespace,
			Labels:          labels,
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(build, v1.GroupVersion.WithKind("Build"))},
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                 corev1.RestartPolicyNever,
			ServiceAccountName:            c.ServiceAccount,
			AutomountServiceAccountToken:  ptr.To(false),
			EnableServiceLinks:            ptr.To(false),
			TerminationGracePeriodSeconds: ptr.To[int64](30),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsUser:      ptr.To(BuilderUID),
				RunAsGroup:     ptr.To(BuilderUID),
				RunAsNonRoot:   ptr.To(true),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			NodeSelector:   c.NodeSelector,
			Tolerations:    c.Tolerations,
			InitContainers: []corev1.Container{c.contextContainer(build)},
			Containers:     []corev1.Container{c.buildContainer(build)},
			Volumes:        c.buildVolumes(),
		},
	}
	for _, name := range c.ImagePullSecrets {
		pod.Spec.ImagePullSecrets = append(pod.Spec.ImagePullSecrets, corev1.LocalObjectReference{Name: name})
	}
	return pod
}

const (
	volBuildWorkspace = "workspace"
	volBuildkitState  = "buildkit"
	volBuildkitRun    = "run"
	volBuildTmp       = "tmp"
	volBuildkitConfig = "buildkitd-config"
)

func (c BuildConfig) buildVolumes() []corev1.Volume {
	vols := []corev1.Volume{}
	for _, name := range []string{volBuildWorkspace, volBuildkitState, volBuildkitRun, volBuildTmp} {
		vols = append(vols, corev1.Volume{Name: name, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
	}
	if c.BuildkitdConfigMap != "" {
		vols = append(vols, corev1.Volume{Name: volBuildkitConfig, VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: c.BuildkitdConfigMap},
			DefaultMode:          ptr.To[int32](0o444),
		}}})
	}
	return vols
}

func (c BuildConfig) contextContainer(build *v1.Build) corev1.Container {
	return corev1.Container{
		Name:            ContainerContext,
		Image:           c.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{"sh", "-c", contextScript},
		Env: []corev1.EnvVar{
			{Name: "EXRADIUS_API_URL", Value: c.APIURL},
			{Name: "EXRADIUS_BUILD_ID", Value: build.Spec.ID},
			{Name: "EXRADIUS_BUILD_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: build.Spec.SecretName},
				Key:                  TokenKey,
			}}},
		},
		VolumeMounts:    []corev1.VolumeMount{{Name: volBuildWorkspace, MountPath: BuildWorkspace}},
		SecurityContext: restricted(),
	}
}

func (c BuildConfig) proxyEnv() []corev1.EnvVar {
	var env []corev1.EnvVar
	for _, kv := range c.proxyVars() {
		env = append(env, corev1.EnvVar{Name: kv[0], Value: kv[1]})
	}
	return env
}

func (c BuildConfig) buildContainer(build *v1.Build) corev1.Container {
	env := []corev1.EnvVar{
		// One build per pod: RUN steps need no sandbox from the builder.
		{Name: "BUILDKITD_FLAGS", Value: ""},
		// The image's TMPDIR is under the read-only home.
		{Name: "TMPDIR", Value: "/tmp"},
		{Name: "DOCKER_CONFIG", Value: "/tmp/docker"},
	}
	flags := "--oci-worker-no-process-sandbox"
	mounts := []corev1.VolumeMount{
		{Name: volBuildWorkspace, MountPath: BuildWorkspace, ReadOnly: true},
		{Name: volBuildkitState, MountPath: buildkitState},
		{Name: volBuildkitRun, MountPath: buildkitRun},
		{Name: volBuildTmp, MountPath: "/tmp"},
	}
	if c.BuildkitdConfigMap != "" {
		flags += " --config " + buildkitConfig + "/buildkitd.toml"
		mounts = append(mounts, corev1.VolumeMount{Name: volBuildkitConfig, MountPath: buildkitConfig, ReadOnly: true})
	}
	env[0].Value = flags
	return corev1.Container{
		Name:            ContainerBuild,
		Image:           c.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         append([]string{"sh", "-c", buildScript, "buildctl"}, c.BuildArgs(build)...),
		Env:             append(env, c.proxyEnv()...),
		Resources:       c.Resources,
		VolumeMounts:    mounts,
		SecurityContext: c.buildSecurityContext(),
	}
}

// buildSecurityContext: rootless BuildKit creates a user namespace (rootlesskit) and mounts
// inside it, which seccomp RuntimeDefault refuses, and maps its subordinate ids with the
// setuid newuidmap (no allowPrivilegeEscalation: false). The runtime's default
// capabilities stay: they bound what root has inside that namespace, which RUN steps need
// (chown). The Localhost profiles are the runtime's defaults plus exactly that; without
// them both seccomp and AppArmor are off.
func (c BuildConfig) buildSecurityContext() *corev1.SecurityContext {
	sc := &corev1.SecurityContext{
		ReadOnlyRootFilesystem: ptr.To(true),
		RunAsNonRoot:           ptr.To(true),
	}
	if c.SeccompProfile != "" {
		sc.SeccompProfile = &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeLocalhost, LocalhostProfile: ptr.To(c.SeccompProfile)}
		if c.AppArmorProfile != "" {
			sc.AppArmorProfile = &corev1.AppArmorProfile{Type: corev1.AppArmorProfileTypeLocalhost, LocalhostProfile: ptr.To(c.AppArmorProfile)}
		}
		return sc
	}
	sc.SeccompProfile = &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeUnconfined}
	sc.AppArmorProfile = &corev1.AppArmorProfile{Type: corev1.AppArmorProfileTypeUnconfined}
	return sc
}
