// exradius-operator turns Runs into pods and Builds into build pods (docs/kubernetes.md §5,
// §7, §12). It holds nothing secret: it references the per-run and per-Build Secrets by
// name and the kubelet resolves the keys.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/controller"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

func main() {
	var (
		runsNamespace, apiURL, identity, serviceAccount        string
		workspaceSize, storageClass, memoryRequest, cpuRequest string
		runtimeClass, nodeSelector, tolerations, pullSecrets   string
		leaderID, leaderNamespace, metricsAddr, probeAddr      string
		buildsNamespace, builderImage, buildServiceAccount     string
		buildkitdConfig, buildResources                        string
		runUID, runGID                                         int64
		userNamespaces, leaderElect                            bool
		workers                                                int
	)
	flag.StringVar(&runsNamespace, "runs-namespace", "exradius-runs", "namespace of Runs, run pods, Secrets and workspaces")
	flag.StringVar(&apiURL, "api-url", "http://exradius-api-internal.exradius:8001", "the api's internal port, as run pods reach it")
	flag.Int64Var(&runUID, "run-uid", 1000, "uid of run containers")
	flag.Int64Var(&runGID, "run-gid", 1000, "gid of run containers and the workspace's fsGroup")
	flag.StringVar(&identity, "identity-configmap", "exradius-run-identity", "ConfigMap with passwd and group for run containers")
	flag.StringVar(&serviceAccount, "service-account", "exradius-run", "ServiceAccount of run pods (its token is never mounted)")
	flag.StringVar(&workspaceSize, "workspace-size", "50Gi", "size of each Run's workspace PVC")
	flag.StringVar(&storageClass, "workspace-storage-class", "", "storage class of workspaces; empty is the cluster default")
	flag.StringVar(&memoryRequest, "memory-request", "1Gi", "memory request of a workstation without declared memory")
	flag.StringVar(&cpuRequest, "cpu-request", "500m", "CPU request of the workstation and setup containers")
	flag.StringVar(&runtimeClass, "runtime-class-name", "", "RuntimeClass of run pods; empty is the cluster default")
	flag.BoolVar(&userNamespaces, "user-namespaces", false, "run pods with hostUsers: false")
	flag.StringVar(&nodeSelector, "node-selector", "{}", "node selector of run pods, as JSON")
	flag.StringVar(&tolerations, "tolerations", "[]", "tolerations of run pods, as JSON")
	flag.StringVar(&pullSecrets, "image-pull-secrets", "", "comma-separated imagePullSecrets of run pods")
	flag.IntVar(&workers, "workers", 4, "Runs reconciled at once (and Builds)")
	flag.StringVar(&buildsNamespace, "builds-namespace", "exradius-builds", "namespace of Builds, build pods and their Secrets")
	flag.StringVar(&builderImage, "builder-image", "", "the rootless BuildKit image of build pods")
	flag.StringVar(&buildServiceAccount, "build-service-account", "exradius-build", "ServiceAccount of build pods (its token is never mounted)")
	flag.StringVar(&buildkitdConfig, "buildkitd-configmap", "", "ConfigMap with buildkitd.toml for build pods; empty: none")
	flag.StringVar(&buildResources, "build-resources", "{}", "resources of the build container, as JSON")
	flag.BoolVar(&leaderElect, "leader-elect", true, "hold a Lease so only one replica reconciles")
	flag.StringVar(&leaderID, "leader-election-id", "exradius-operator", "name of the leader election Lease")
	flag.StringVar(&leaderNamespace, "leader-election-namespace", "exradius", "namespace of the leader election Lease")
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "address of /metrics; 0 turns it off")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "address of /healthz and /readyz")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	logger := ctrl.Log.WithName("setup")

	config := podspec.Config{
		APIURL: apiURL, RunUID: runUID, RunGID: runGID, IdentityConfigMap: identity,
		ServiceAccount: serviceAccount, WorkspaceStorageClass: storageClass,
		RuntimeClassName: runtimeClass, UserNamespaces: userNamespaces,
	}
	var err error
	for _, q := range []struct {
		name, value string
		into        *resource.Quantity
	}{
		{"workspace-size", workspaceSize, &config.WorkspaceSize},
		{"memory-request", memoryRequest, &config.MemoryRequest},
		{"cpu-request", cpuRequest, &config.CPURequest},
	} {
		if q.value == "" {
			continue
		}
		if *q.into, err = resource.ParseQuantity(q.value); err != nil {
			fail(logger, fmt.Errorf("--%s: %w", q.name, err))
		}
	}
	if err = json.Unmarshal([]byte(orDefault(nodeSelector, "{}")), &config.NodeSelector); err != nil {
		fail(logger, fmt.Errorf("--node-selector: %w", err))
	}
	if err = json.Unmarshal([]byte(orDefault(tolerations, "[]")), &config.Tolerations); err != nil {
		fail(logger, fmt.Errorf("--tolerations: %w", err))
	}
	for _, name := range strings.Split(pullSecrets, ",") {
		if name = strings.TrimSpace(name); name != "" {
			config.ImagePullSecrets = append(config.ImagePullSecrets, name)
		}
	}

	if builderImage == "" {
		fail(logger, fmt.Errorf("--builder-image is required"))
	}
	builds := podspec.BuildConfig{
		APIURL: apiURL, Image: builderImage, ServiceAccount: buildServiceAccount,
		BuildkitdConfigMap: buildkitdConfig, NodeSelector: config.NodeSelector, Tolerations: config.Tolerations,
	}
	if err = json.Unmarshal([]byte(orDefault(buildResources, "{}")), &builds.Resources); err != nil {
		fail(logger, fmt.Errorf("--build-resources: %w", err))
	}

	scheme := runtime.NewScheme()
	utilRuntime(clientgoscheme.AddToScheme(scheme))
	utilRuntime(v1.AddToScheme(scheme))

	restConfig := ctrl.GetConfigOrDie()
	mgr, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress:  probeAddr,
		LeaderElection:          leaderElect,
		LeaderElectionID:        leaderID,
		LeaderElectionNamespace: leaderNamespace,
		// Release the Lease on a clean exit, so a new replica takes over at once.
		LeaderElectionReleaseOnCancel: true,
		// Runs and PVCs live in the runs namespace, Builds in the builds namespace, pods in
		// both; nothing else is watched (and RBAC allows no more).
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{runsNamespace: {}, buildsNamespace: {}},
			ByObject: map[client.Object]cache.ByObject{
				&v1.Run{}:                       {Namespaces: map[string]cache.Config{runsNamespace: {}}},
				&corev1.PersistentVolumeClaim{}: {Namespaces: map[string]cache.Config{runsNamespace: {}}},
				&v1.Build{}:                     {Namespaces: map[string]cache.Config{buildsNamespace: {}}},
			},
		},
	})
	if err != nil {
		fail(logger, err)
	}
	clientset, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		fail(logger, err)
	}
	reconciler := &controller.RunReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Scheme:    scheme,
		Builder:   podspec.Pods{Config: config},
		Logs:      controller.ClientsetLogs{Clientset: clientset},
		Recorder:  mgr.GetEventRecorder("exradius-operator"),
	}
	if err = reconciler.SetupWithManager(mgr, workers); err != nil {
		fail(logger, err)
	}
	buildReconciler := &controller.BuildReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Builder:   builds,
		Logs:      controller.ClientsetLogs{Clientset: clientset},
		Recorder:  mgr.GetEventRecorder("exradius-operator"),
	}
	if err = buildReconciler.SetupWithManager(mgr, workers); err != nil {
		fail(logger, err)
	}
	utilRuntime(mgr.AddHealthzCheck("healthz", healthz.Ping))
	utilRuntime(mgr.AddReadyzCheck("readyz", healthz.Ping))

	logger.Info("starting", "runsNamespace", runsNamespace, "buildsNamespace", buildsNamespace, "apiURL", apiURL)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		fail(logger, err)
	}
}

func orDefault(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func utilRuntime(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func fail(logger interface{ Error(error, string, ...any) }, err error) {
	logger.Error(err, "cannot start")
	os.Exit(1)
}
