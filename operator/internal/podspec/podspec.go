// Package podspec turns a Run into the objects that execute it: the workspace PVC and the
// pod (docs/kubernetes.md §7). Everything here is a pure function of the Run and the
// operator's configuration. The security context is written here and nothing in a Run
// can change it (§7.8).
package podspec

import (
	"fmt"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
)

// Paths inside run containers.
const (
	RuntimeDir      = "/opt/exradius/runtime"
	Runner          = RuntimeDir + "/bin/exradius-runner"
	WorkDir         = "/work"
	HomeDir         = "/home/runner"
	StateDir        = "/exradius/state"
	WorkspaceDir    = "/exradius/workspace"
	AttachmentsDir  = WorkDir + "/.exradius/attachments"
	FilesMountDir   = "/exradius/attachments"
	attachmentsPath = "work/.exradius/attachments"
)

// Container names.
const (
	ContainerPrepare     = "prepare"
	ContainerFiles       = "files"
	ContainerWorkstation = "workstation"
)

// Labels and annotations.
const (
	LabelRun           = "exradius.dev/run"
	LabelProject       = "exradius.dev/project"
	LabelPurpose       = "exradius.dev/purpose"
	LabelPodGeneration = "exradius.dev/pod-generation"
	AnnotationTaskID   = "exradius.dev/task-id"
)

// TokenKey is the Secret key of the run token.
const TokenKey = "token"

// Config is the operator's part of every pod, from the chart's values.
type Config struct {
	APIURL                string
	RunUID                int64
	RunGID                int64
	IdentityConfigMap     string
	ServiceAccount        string
	WorkspaceSize         resource.Quantity
	WorkspaceStorageClass string
	MemoryRequest         resource.Quantity
	CPURequest            resource.Quantity
	RuntimeClassName      string
	UserNamespaces        bool
	NodeSelector          map[string]string
	Tolerations           []corev1.Toleration
	ImagePullSecrets      []string
}

// Builder makes the objects of a Run. Pods is the only backend; an agent-sandbox backend
// would be another implementation (§22).
type Builder interface {
	// Workspace is the Run's PVC, or nil when the Run has none (probes).
	Workspace(run *v1.Run) *corev1.PersistentVolumeClaim
	// Pod is the pod of the Run's current pod generation.
	Pod(run *v1.Run) *corev1.Pod
}

// Pods builds a plain pod and a PVC per Run.
type Pods struct{ Config Config }

var _ Builder = Pods{}

// PodName is the pod of one pod generation of a Run.
func PodName(run *v1.Run, generation int64) string {
	return fmt.Sprintf("%s-%d", run.Name, generation)
}

// CloneContainer and SetupContainer name the init containers by index: folder names and
// commands are not DNS labels.
func CloneContainer(n int) string { return fmt.Sprintf("clone-%d", n) }
func SetupContainer(n int) string { return fmt.Sprintf("setup-%d", n) }

// StepNames are the steps of a Run's pod in order, by container name.
func StepNames(run *v1.Run) []v1.Step {
	if run.Spec.Purpose == v1.PurposeProbe {
		return nil
	}
	steps := []v1.Step{{Name: "Prepare workspace", Container: ContainerPrepare}}
	for n, repo := range run.Spec.Repositories {
		branch := repo.Branch
		if branch == "" {
			branch = "default branch"
		}
		steps = append(steps, v1.Step{Name: fmt.Sprintf("Clone %s (%s)", repo.Folder, branch), Container: CloneContainer(n)})
	}
	for n, command := range run.Spec.Workstation.Setup {
		steps = append(steps, v1.Step{Name: "Setup: " + command, Container: SetupContainer(n)})
	}
	for i := range steps {
		steps[i].State = v1.StepPending
	}
	return steps
}

func labels(run *v1.Run) map[string]string {
	l := map[string]string{LabelRun: run.Name, LabelPurpose: string(run.Spec.Purpose)}
	if run.Spec.Project != "" {
		l[LabelProject] = run.Spec.Project
	}
	return l
}

func ownerRef(run *v1.Run) metav1.OwnerReference {
	return *metav1.NewControllerRef(run, v1.GroupVersion.WithKind("Run"))
}

// Workspace implements Builder.
func (b Pods) Workspace(run *v1.Run) *corev1.PersistentVolumeClaim {
	if run.Spec.Purpose == v1.PurposeProbe {
		return nil
	}
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:            run.Name,
			Namespace:       run.Namespace,
			Labels:          labels(run),
			OwnerReferences: []metav1.OwnerReference{ownerRef(run)},
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: b.Config.WorkspaceSize},
			},
		},
	}
	if b.Config.WorkspaceStorageClass != "" {
		pvc.Spec.StorageClassName = ptr.To(b.Config.WorkspaceStorageClass)
	}
	return pvc
}

// Pod implements Builder.
func (b Pods) Pod(run *v1.Run) *corev1.Pod {
	c := b.Config
	gen := run.Spec.PodGeneration
	podLabels := labels(run)
	podLabels[LabelPodGeneration] = strconv.FormatInt(gen, 10)
	var annotations map[string]string
	if run.Spec.TaskID != "" {
		annotations = map[string]string{AnnotationTaskID: run.Spec.TaskID}
	}

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:            PodName(run, gen),
			Namespace:       run.Namespace,
			Labels:          podLabels,
			Annotations:     annotations,
			OwnerReferences: []metav1.OwnerReference{ownerRef(run)},
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                 corev1.RestartPolicyNever,
			ServiceAccountName:            c.ServiceAccount,
			AutomountServiceAccountToken:  ptr.To(false),
			EnableServiceLinks:            ptr.To(false),
			TerminationGracePeriodSeconds: ptr.To[int64](30),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsUser:           ptr.To(c.RunUID),
				RunAsGroup:          ptr.To(c.RunGID),
				RunAsNonRoot:        ptr.To(true),
				FSGroup:             ptr.To(c.RunGID),
				FSGroupChangePolicy: ptr.To(corev1.FSGroupChangeOnRootMismatch),
				SeccompProfile:      &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			NodeSelector: c.NodeSelector,
			Tolerations:  c.Tolerations,
			Volumes:      b.volumes(run),
		},
	}
	if c.RuntimeClassName != "" {
		pod.Spec.RuntimeClassName = ptr.To(c.RuntimeClassName)
	}
	if c.UserNamespaces {
		pod.Spec.HostUsers = ptr.To(false)
	}
	for _, name := range c.ImagePullSecrets {
		pod.Spec.ImagePullSecrets = append(pod.Spec.ImagePullSecrets, corev1.LocalObjectReference{Name: name})
	}

	if run.Spec.Purpose == v1.PurposeProbe {
		pod.Spec.Containers = []corev1.Container{b.probeWorkstation(run)}
		return pod
	}
	pod.Spec.InitContainers = append(pod.Spec.InitContainers, b.prepare(run))
	for n := range run.Spec.Repositories {
		pod.Spec.InitContainers = append(pod.Spec.InitContainers, b.clone(run, n))
	}
	for n := range run.Spec.Workstation.Setup {
		pod.Spec.InitContainers = append(pod.Spec.InitContainers, b.setup(run, n))
	}
	if run.Spec.Purpose == v1.PurposeTask {
		pod.Spec.InitContainers = append(pod.Spec.InitContainers, b.files(run))
	}
	pod.Spec.Containers = []corev1.Container{b.workstation(run)}
	return pod
}

const (
	volWorkspace = "workspace"
	volRuntime   = "runtime"
	volIdentity  = "identity"
	volFilesTmp  = "files-tmp"
	volHome      = "home"
	volTmp       = "tmp"
	volState     = "state"
)

func (b Pods) volumes(run *v1.Run) []corev1.Volume {
	vols := []corev1.Volume{
		{Name: volRuntime, VolumeSource: corev1.VolumeSource{Image: &corev1.ImageVolumeSource{
			Reference: run.Spec.Runtime.Image, PullPolicy: corev1.PullIfNotPresent,
		}}},
		{Name: volIdentity, VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: b.Config.IdentityConfigMap},
			DefaultMode:          ptr.To[int32](0o444),
		}}},
	}
	if run.Spec.Purpose == v1.PurposeProbe {
		for _, name := range []string{volHome, volTmp, volState} {
			vols = append(vols, corev1.Volume{Name: name, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		}
		return vols
	}
	vols = append([]corev1.Volume{{Name: volWorkspace, VolumeSource: corev1.VolumeSource{
		PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: run.Name},
	}}}, vols...)
	if run.Spec.Purpose == v1.PurposeTask {
		vols = append(vols, corev1.Volume{Name: volFilesTmp, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
	}
	return vols
}

func securityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		ReadOnlyRootFilesystem:   ptr.To(true),
		AllowPrivilegeEscalation: ptr.To(false),
		RunAsNonRoot:             ptr.To(true),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
}

func runtimeMount() corev1.VolumeMount {
	return corev1.VolumeMount{Name: volRuntime, MountPath: RuntimeDir, ReadOnly: true}
}

func identityMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{Name: volIdentity, MountPath: "/etc/passwd", SubPath: "passwd", ReadOnly: true},
		{Name: volIdentity, MountPath: "/etc/group", SubPath: "group", ReadOnly: true},
	}
}

// workspaceMounts are /work, the home, /tmp and the state directory; with attachments
// also the read-only attachments directory over /work/.exradius/attachments.
func workspaceMounts(attachments bool) []corev1.VolumeMount {
	mounts := []corev1.VolumeMount{
		{Name: volWorkspace, MountPath: WorkDir, SubPath: "work"},
	}
	if attachments {
		mounts = append(mounts, corev1.VolumeMount{Name: volWorkspace, MountPath: AttachmentsDir, SubPath: attachmentsPath, ReadOnly: true})
	}
	return append(mounts,
		corev1.VolumeMount{Name: volWorkspace, MountPath: HomeDir, SubPath: "home"},
		corev1.VolumeMount{Name: volWorkspace, MountPath: "/tmp", SubPath: "tmp"},
		corev1.VolumeMount{Name: volWorkspace, MountPath: StateDir, SubPath: "state"},
	)
}

func (b Pods) baseEnv(run *v1.Run) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "EXRADIUS_API_URL", Value: b.Config.APIURL},
		secretEnv(run, "EXRADIUS_RUN_TOKEN", TokenKey),
		{Name: "EXRADIUS_PURPOSE", Value: string(run.Spec.Purpose)},
		{Name: "EXRADIUS_ID", Value: run.Spec.ID},
	}
}

func pathEnv(workDir string) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "HOME", Value: HomeDir},
		{Name: "EXRADIUS_STATE_DIR", Value: StateDir},
		{Name: "EXRADIUS_WORK_DIR", Value: workDir},
	}
}

func secretEnv(run *v1.Run, name, key string) corev1.EnvVar {
	return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: run.Spec.SecretName},
		Key:                  key,
	}}}
}

func specEnv(run *v1.Run, vars []v1.EnvVar) []corev1.EnvVar {
	out := make([]corev1.EnvVar, 0, len(vars))
	for _, v := range vars {
		if v.SecretKey != "" {
			out = append(out, secretEnv(run, v.Name, v.SecretKey))
		} else {
			out = append(out, corev1.EnvVar{Name: v.Name, Value: v.Value})
		}
	}
	return out
}

// mergeEnv joins env lists in order; a later variable replaces an earlier one of the same
// name, at the earlier one's position.
func mergeEnv(lists ...[]corev1.EnvVar) []corev1.EnvVar {
	var out []corev1.EnvVar
	index := map[string]int{}
	for _, list := range lists {
		for _, v := range list {
			if i, ok := index[v.Name]; ok {
				out[i] = v
				continue
			}
			index[v.Name] = len(out)
			out = append(out, v)
		}
	}
	return out
}

// workstationResources is request = limit for declared memory, else the default request
// and no limit; a CPU request and never a CPU limit (§6).
func (b Pods) workstationResources(run *v1.Run) corev1.ResourceRequirements {
	r := corev1.ResourceRequirements{Requests: corev1.ResourceList{}}
	if !b.Config.CPURequest.IsZero() {
		r.Requests[corev1.ResourceCPU] = b.Config.CPURequest
	}
	if m := run.Spec.Workstation.Memory; m != nil && !m.IsZero() {
		r.Requests[corev1.ResourceMemory] = *m
		r.Limits = corev1.ResourceList{corev1.ResourceMemory: *m}
	} else if !b.Config.MemoryRequest.IsZero() {
		r.Requests[corev1.ResourceMemory] = b.Config.MemoryRequest
	}
	return r
}

func (b Pods) prepare(run *v1.Run) corev1.Container {
	return corev1.Container{
		Name:            ContainerPrepare,
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{Runner, "--prepare", string(run.Spec.Purpose), run.Spec.ID},
		Env:             b.baseEnv(run),
		VolumeMounts: []corev1.VolumeMount{
			{Name: volWorkspace, MountPath: WorkspaceDir},
			runtimeMount(),
		},
		SecurityContext: securityContext(),
	}
}

func (b Pods) clone(run *v1.Run, n int) corev1.Container {
	repo := run.Spec.Repositories[n]
	env := []corev1.EnvVar{
		{Name: "EXRADIUS_CLONE_FOLDER", Value: repo.Folder},
		{Name: "EXRADIUS_CLONE_URL", Value: repo.URL},
		{Name: "EXRADIUS_CLONE_BRANCH", Value: repo.Branch},
		{Name: "EXRADIUS_CLONE_INDEX", Value: strconv.Itoa(n)},
	}
	if repo.CredentialKey != "" {
		env = append(env, secretEnv(run, "EXRADIUS_GIT_AUTH", repo.CredentialKey))
	}
	return corev1.Container{
		Name:            CloneContainer(n),
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{Runner, "--clone"},
		WorkingDir:      WorkDir,
		Env:             mergeEnv(b.baseEnv(run), pathEnv(WorkDir), env),
		VolumeMounts:    append(append(workspaceMounts(false), runtimeMount()), identityMounts()...),
		SecurityContext: securityContext(),
	}
}

func (b Pods) setup(run *v1.Run, n int) corev1.Container {
	return corev1.Container{
		Name:            SetupContainer(n),
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{"/bin/sh", "-c", run.Spec.Workstation.Setup[n]},
		WorkingDir:      WorkDir,
		Env:             mergeEnv(specEnv(run, run.Spec.Workstation.Env), pathEnv(WorkDir), specEnv(run, run.Spec.Git.SetupEnv)),
		Resources:       b.workstationResources(run),
		VolumeMounts:    append(workspaceMounts(true), identityMounts()...),
		SecurityContext: securityContext(),
	}
}

// files writes answer attachments as they arrive (§8.2), as a uid other than the run
// user's, into the directory the workstation mounts read-only.
func (b Pods) files(run *v1.Run) corev1.Container {
	sc := securityContext()
	sc.RunAsUser = ptr.To(b.Config.RunUID + 1)
	sc.RunAsGroup = ptr.To(b.Config.RunGID)
	return corev1.Container{
		Name:            ContainerFiles,
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{Runner, "--files", run.Spec.ID},
		RestartPolicy:   ptr.To(corev1.ContainerRestartPolicyAlways),
		Env: mergeEnv(b.baseEnv(run), []corev1.EnvVar{
			{Name: "HOME", Value: "/tmp"},
			{Name: "EXRADIUS_ATTACHMENTS_DIR", Value: FilesMountDir},
		}),
		VolumeMounts: []corev1.VolumeMount{
			{Name: volWorkspace, MountPath: FilesMountDir, SubPath: attachmentsPath},
			runtimeMount(),
			{Name: volFilesTmp, MountPath: "/tmp"},
		},
		SecurityContext: sc,
	}
}

func (b Pods) runnerArgs(run *v1.Run) []string {
	switch run.Spec.Purpose {
	case v1.PurposeVerify:
		return []string{Runner, "--verify", run.Spec.ID}
	case v1.PurposeProbe:
		args := []string{Runner, "--probe", run.Spec.Runtime.Name, "--report", run.Spec.ID}
		if run.Spec.Model != "" {
			args = append(args, "--model", run.Spec.Model)
		}
		return args
	default:
		return []string{Runner, run.Spec.ID}
	}
}

func (b Pods) workstation(run *v1.Run) corev1.Container {
	return corev1.Container{
		Name:            ContainerWorkstation,
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         b.runnerArgs(run),
		WorkingDir:      WorkDir,
		Env: mergeEnv(
			specEnv(run, run.Spec.Workstation.Env),
			pathEnv(WorkDir),
			specEnv(run, run.Spec.Git.Env),
			specEnv(run, run.Spec.Runtime.Env),
			b.baseEnv(run),
		),
		Resources:       b.workstationResources(run),
		VolumeMounts:    append(append(workspaceMounts(true), runtimeMount()), identityMounts()...),
		SecurityContext: securityContext(),
	}
}

func (b Pods) probeWorkstation(run *v1.Run) corev1.Container {
	return corev1.Container{
		Name:            ContainerWorkstation,
		Image:           run.Spec.Workstation.Image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         b.runnerArgs(run),
		WorkingDir:      HomeDir,
		Env: mergeEnv(
			specEnv(run, run.Spec.Workstation.Env),
			pathEnv(HomeDir),
			specEnv(run, run.Spec.Runtime.Env),
			b.baseEnv(run),
		),
		Resources: b.workstationResources(run),
		VolumeMounts: append([]corev1.VolumeMount{
			{Name: volHome, MountPath: HomeDir},
			{Name: volTmp, MountPath: "/tmp"},
			{Name: volState, MountPath: StateDir},
			runtimeMount(),
		}, identityMounts()...),
		SecurityContext: securityContext(),
	}
}
