package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func env(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func get(t *testing.T, name, server string, getenv func(string) string) (Credentials, error) {
	t.Helper()
	var out bytes.Buffer
	err := run(name, []string{"get"}, strings.NewReader(server+"\n"), &out, getenv)
	var creds Credentials
	if err == nil {
		if e := json.Unmarshal(out.Bytes(), &creds); e != nil {
			t.Fatalf("output %q: %v", out.String(), e)
		}
	}
	return creds, err
}

func TestGoogleAsksTheMetadataServerDirectly(t *testing.T) {
	var seen *http.Request
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r
		io.WriteString(w, `{"access_token":"ya29.token","expires_in":3599,"token_type":"Bearer"}`)
	}))
	defer metadata.Close()
	// A proxy in the environment is not used for the metadata server.
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	creds, err := get(t, "docker-credential-exradius-gcr", "europe-west4-docker.pkg.dev",
		env(map[string]string{"GCE_METADATA_HOST": strings.TrimPrefix(metadata.URL, "http://")}))
	if err != nil {
		t.Fatal(err)
	}
	want := Credentials{ServerURL: "europe-west4-docker.pkg.dev", Username: "oauth2accesstoken", Secret: "ya29.token"}
	if creds != want {
		t.Fatalf("got %+v", creds)
	}
	if seen.URL.Path != "/computeMetadata/v1/instance/service-accounts/default/token" || seen.Header.Get("Metadata-Flavor") != "Google" {
		t.Fatalf("request %s %v", seen.URL, seen.Header)
	}
}

func TestGoogleReportsAMetadataServerWithoutAnIdentity(t *testing.T) {
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "service account not found", http.StatusNotFound)
	}))
	defer metadata.Close()
	_, err := get(t, "docker-credential-exradius-gcr", "europe-west4-docker.pkg.dev",
		env(map[string]string{"GCE_METADATA_HOST": strings.TrimPrefix(metadata.URL, "http://")}))
	if err == nil || !strings.Contains(err.Error(), "404") {
		t.Fatalf("err = %v", err)
	}
}

func TestAzureExchangesTheFederatedTokenForARefreshToken(t *testing.T) {
	var forms []map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Fatal(err)
		}
		form := map[string]string{"path": r.URL.Path}
		for k := range r.PostForm {
			form[k] = r.PostForm.Get(k)
		}
		forms = append(forms, form)
		switch r.URL.Path {
		case "/tenant-id/oauth2/v2.0/token":
			io.WriteString(w, `{"access_token":"arm-token","expires_in":3599}`)
		case "/oauth2/exchange":
			io.WriteString(w, `{"refresh_token":"acr-refresh"}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	file := filepath.Join(t.TempDir(), "azure-identity-token")
	if err := os.WriteFile(file, []byte("federated\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	creds, err := get(t, "docker-credential-exradius-acr", server.URL, env(map[string]string{
		"AZURE_CLIENT_ID": "client-id", "AZURE_TENANT_ID": "tenant-id", "AZURE_FEDERATED_TOKEN_FILE": file,
		"AZURE_AUTHORITY_HOST": server.URL + "/",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if creds.Username != ACRUser || creds.Secret != "acr-refresh" || creds.ServerURL != server.URL {
		t.Fatalf("got %+v", creds)
	}
	if len(forms) != 2 {
		t.Fatalf("requests %v", forms)
	}
	aad, exchange := forms[0], forms[1]
	if aad["client_id"] != "client-id" || aad["client_assertion"] != "federated" || aad["scope"] != "https://management.azure.com/.default" ||
		aad["grant_type"] != "client_credentials" || aad["client_assertion_type"] != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer" {
		t.Fatalf("token request %v", aad)
	}
	host := strings.TrimPrefix(server.URL, "http://")
	if exchange["grant_type"] != "access_token" || exchange["service"] != host || exchange["tenant"] != "tenant-id" || exchange["access_token"] != "arm-token" {
		t.Fatalf("exchange %v", exchange)
	}
}

func TestAzureNeedsTheWebhook(t *testing.T) {
	_, err := get(t, "docker-credential-exradius-acr", "x.azurecr.io", env(nil))
	if err == nil || !strings.Contains(err.Error(), "azure.workload.identity/use") {
		t.Fatalf("err = %v", err)
	}
}

func TestOtherCommandsAndNames(t *testing.T) {
	var out bytes.Buffer
	if err := run("docker-credential-exradius-acr", []string{"list"}, strings.NewReader(""), &out, env(nil)); err != nil || out.String() != "{}\n" {
		t.Fatalf("list: %q %v", out.String(), err)
	}
	if err := run("docker-credential-exradius-gcr", []string{"store"}, strings.NewReader("{}"), &out, env(nil)); err != nil {
		t.Fatalf("store: %v", err)
	}
	if _, err := get(t, "docker-credential-exradius", "x", env(nil)); err == nil {
		t.Fatal("the bare name picks no cloud")
	}
	if err := run("docker-credential-exradius-gcr", nil, nil, &out, env(nil)); err == nil {
		t.Fatal("usage")
	}
	if base, host := registryBase("exradius.azurecr.io"); base != "https://exradius.azurecr.io" || host != "exradius.azurecr.io" {
		t.Fatalf("%s %s", base, host)
	}
}
