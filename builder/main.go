// docker-credential-exradius is the Docker credential helper of the Exradius builder image
// for Artifact Registry and ACR (docs/kubernetes.md §12): a build pod signs in to the
// registry it pushes to with its own cloud identity, so no registry password is ever in a
// Secret. ECR uses Amazon's docker-credential-ecr-login instead.
//
// It is installed under two names, and the name picks the cloud:
//
//   - docker-credential-exradius-gcr: GKE Workload Identity. The GKE metadata server gives
//     the pod's Google access token, which Artifact Registry takes as the password of
//     "oauth2accesstoken".
//   - docker-credential-exradius-acr: AKS Workload Identity. The federated ServiceAccount
//     token its webhook mounts is exchanged with Entra ID for an Azure Resource Manager
//     token, and that at the registry's /oauth2/exchange for an ACR refresh token, the
//     password of the user 00000000-0000-0000-0000-000000000000.
//
// Only "get" does anything: buildctl asks for the push credentials of one registry. The
// standard library only, like the egress proxy.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Credentials are what a Docker credential helper's "get" prints.
type Credentials struct {
	ServerURL string `json:"ServerURL"`
	Username  string `json:"Username"`
	Secret    string `json:"Secret"`
}

// ACRUser is the user name of an ACR refresh token.
const ACRUser = "00000000-0000-0000-0000-000000000000"

func main() {
	if err := run(filepath.Base(os.Args[0]), os.Args[1:], os.Stdin, os.Stdout, os.Getenv); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(name string, args []string, stdin io.Reader, stdout io.Writer, getenv func(string) string) error {
	if len(args) != 1 {
		return fmt.Errorf("usage: %s get|store|erase|list", name)
	}
	switch args[0] {
	case "get":
	case "list":
		_, err := io.WriteString(stdout, "{}\n")
		return err
	case "store", "erase":
		return nil // credentials come from the pod's identity; there is nothing to keep
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
	line, err := bufio.NewReader(stdin).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	server := strings.TrimSpace(line)
	if server == "" {
		return errors.New("no server URL on stdin")
	}
	client := &http.Client{Timeout: 30 * time.Second}
	var creds Credentials
	switch {
	case strings.HasSuffix(name, "-gcr"):
		creds, err = google(client, server, getenv)
	case strings.HasSuffix(name, "-acr"):
		creds, err = azure(client, server, getenv)
	default:
		return fmt.Errorf("%s: install it as docker-credential-exradius-gcr or -acr", name)
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(stdout).Encode(creds)
}

// google asks the metadata server for the pod's access token. The metadata server answers
// only the pod itself, so the request never goes through the egress proxy.
func google(client *http.Client, server string, getenv func(string) string) (Credentials, error) {
	host := getenv("GCE_METADATA_HOST")
	if host == "" {
		host = "169.254.169.254"
	}
	direct := *client
	direct.Transport = &http.Transport{Proxy: nil}
	req, _ := http.NewRequest(http.MethodGet, "http://"+host+"/computeMetadata/v1/instance/service-accounts/default/token", nil)
	req.Header.Set("Metadata-Flavor", "Google")
	var token struct {
		AccessToken string `json:"access_token"`
	}
	if err := doJSON(&direct, req, &token, "the GKE metadata server"); err != nil {
		return Credentials{}, err
	}
	return Credentials{ServerURL: server, Username: "oauth2accesstoken", Secret: token.AccessToken}, nil
}

// azure exchanges the AKS Workload Identity token for an ACR refresh token. Entra ID and
// the registry are public hosts, reached through the egress proxy (HTTPS_PROXY).
func azure(client *http.Client, server string, getenv func(string) string) (Credentials, error) {
	clientID, tenant, file := getenv("AZURE_CLIENT_ID"), getenv("AZURE_TENANT_ID"), getenv("AZURE_FEDERATED_TOKEN_FILE")
	if clientID == "" || tenant == "" || file == "" {
		return Credentials{}, errors.New("AKS Workload Identity is not set up for this pod: AZURE_CLIENT_ID, AZURE_TENANT_ID " +
			"or AZURE_FEDERATED_TOKEN_FILE is missing (the build ServiceAccount's azure.workload.identity/client-id " +
			"annotation, the pod's azure.workload.identity/use label)")
	}
	assertion, err := os.ReadFile(file)
	if err != nil {
		return Credentials{}, err
	}
	authority := strings.TrimRight(getenv("AZURE_AUTHORITY_HOST"), "/")
	if authority == "" {
		authority = "https://login.microsoftonline.com"
	}
	proxied := *client
	proxied.Transport = &http.Transport{Proxy: http.ProxyFromEnvironment}

	form := url.Values{
		"client_id":             {clientID},
		"scope":                 {"https://management.azure.com/.default"},
		"grant_type":            {"client_credentials"},
		"client_assertion_type": {"urn:ietf:params:oauth:client-assertion-type:jwt-bearer"},
		"client_assertion":      {strings.TrimSpace(string(assertion))},
	}
	req, _ := http.NewRequest(http.MethodPost, authority+"/"+url.PathEscape(tenant)+"/oauth2/v2.0/token", strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	var aad struct {
		AccessToken string `json:"access_token"`
	}
	if err := doJSON(&proxied, req, &aad, "Entra ID"); err != nil {
		return Credentials{}, err
	}

	base, registry := registryBase(server)
	form = url.Values{"grant_type": {"access_token"}, "service": {registry}, "tenant": {tenant}, "access_token": {aad.AccessToken}}
	req, _ = http.NewRequest(http.MethodPost, base+"/oauth2/exchange", strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	var exchange struct {
		RefreshToken string `json:"refresh_token"`
	}
	if err := doJSON(&proxied, req, &exchange, "the ACR token exchange"); err != nil {
		return Credentials{}, err
	}
	if exchange.RefreshToken == "" {
		return Credentials{}, errors.New("the ACR token exchange returned no refresh token")
	}
	return Credentials{ServerURL: server, Username: ACRUser, Secret: exchange.RefreshToken}, nil
}

// registryBase is the registry's base URL (https unless the server URL names a scheme)
// and its host, the "service" of the exchange.
func registryBase(server string) (base, host string) {
	if !strings.Contains(server, "://") {
		server = "https://" + server
	}
	u, err := url.Parse(server)
	if err != nil || u.Host == "" {
		return strings.TrimRight(server, "/"), strings.TrimPrefix(server, "https://")
	}
	return u.Scheme + "://" + u.Host, u.Host
}

func doJSON(client *http.Client, req *http.Request, into any, what string) error {
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: %s %s", what, resp.Status, strings.TrimSpace(string(body[:min(len(body), 300)])))
	}
	if err := json.Unmarshal(body, into); err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	return nil
}
