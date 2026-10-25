variable "ssh_allowed_cidrs" {
  description = "IPv4 CIDRs, each /24 or narrower, allowed to reach port 22 on every node (the only inbound rule). Set as a workspace variable."
  type        = list(string)

  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0 && alltrue([for c in var.ssh_allowed_cidrs : can(cidrnetmask(c)) && can(regex("/(2[4-9]|3[0-2])$", c))])
    error_message = "ssh_allowed_cidrs must be a non-empty list of IPv4 CIDRs, each /24 or narrower, for example 203.0.113.7/32."
  }
}

variable "deploy_ssh_public_key" {
  description = "Public key Ansible uses to reach the nodes (ed25519, or ed25519 on a FIDO2 token). Set as a workspace variable."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|sk-ssh-ed25519@openssh\\.com) ", var.deploy_ssh_public_key))
    error_message = "deploy_ssh_public_key must be an OpenSSH ed25519 public key (starts with \"ssh-ed25519 \" or \"sk-ssh-ed25519@openssh.com \")."
  }
}

variable "location" {
  description = "Hetzner location for the servers, primary IPs and volume."
  type        = string
  default     = "nbg1"
}

variable "server_type" {
  description = "Server type for all three nodes. CAX21 is ARM64: CX33 was not orderable in any EU location on 2026-10-12."
  type        = string
  default     = "cax21"
}
