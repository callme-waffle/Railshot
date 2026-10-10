#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root/infrastructure/ansible"
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-inventory -i inventories/example/hosts.yml --graph
for playbook in playbooks/*.yml; do
  ansible-playbook -i inventories/example/hosts.yml "$playbook" --syntax-check
done
ansible-playbook -i inventory/hosts.example.yml site.yml --syntax-check
ansible-playbook -i localhost, tests/render.yml
ansible-lint --offline playbooks roles inventories/example
python3 "$repo_root/ci/scripts/check_contract.py"
python3 -m pytest "$repo_root/ci/tests" -q
python3 -m unittest test_database.NativeBoundaries -v
