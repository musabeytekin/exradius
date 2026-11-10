plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "azurerm" {
  enabled = true
  version = "0.33.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# The install is meant to be destroyed with terraform destroy (a test, or a rebuild); the
# data that matters lives in the database, whose backups Azure keeps.
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = false
}
