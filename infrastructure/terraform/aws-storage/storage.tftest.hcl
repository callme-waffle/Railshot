mock_provider "aws" {}
variables {
  cluster_id = "demo"
  region     = "ap-northeast-2"
  account_id = "123456789012"
  vpc_id     = "vpc-0123456789abcdef0"
  workers = {
    compute-a = { instance_id = "i-0123456789abcdef0", private_ip = "10.0.1.10", az = "ap-northeast-2a", subnet_id = "subnet-0123456789abcdef0" }
    compute-b = { instance_id = "i-0123456789abcdef1", private_ip = "10.0.2.10", az = "ap-northeast-2c", subnet_id = "subnet-0123456789abcdef1" }
  }
}
run "regional_shared_backend" {
  command = plan
  override_data {
    target = data.aws_instance.worker["compute-a"]
    values = { instance_state = "running", private_ip = "10.0.1.10", availability_zone = "ap-northeast-2a", subnet_id = "subnet-0123456789abcdef0" }
  }
  override_data {
    target = data.aws_instance.worker["compute-b"]
    values = { instance_state = "running", private_ip = "10.0.2.10", availability_zone = "ap-northeast-2c", subnet_id = "subnet-0123456789abcdef1" }
  }
  override_data {
    target = data.aws_subnet.worker["compute-a"]
    values = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2a" }
  }
  override_data {
    target = data.aws_subnet.worker["compute-b"]
    values = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2c" }
  }
  assert {
    condition     = aws_efs_file_system.cluster.encrypted
    error_message = "The filesystem must be encrypted; Regional scope is observed before use."
  }
  assert {
    condition     = length(aws_efs_mount_target.worker_az) == 2 && aws_efs_file_system.cluster.tags["railshot-cluster"] == "demo"
    error_message = "Each worker AZ requires an owned mount target."
  }
  assert {
    condition     = length(one(aws_security_group.nfs.ingress).cidr_blocks) == 2 && contains(one(aws_security_group.nfs.ingress).cidr_blocks, "10.0.1.10/32") && one(aws_security_group.nfs.ingress).from_port == 2049
    error_message = "NFS access must be scoped to admitted worker addresses."
  }
}
