#!/usr/bin/env python3
"""Generate synthetic `aws ec2 describe-instances` snapshots as Parquet.

This is the stand-in for the "ingestion" step (GitHub Actions running `aws ec2
describe-instances` every 2 hours). One file per region every 2 hours, zstd-compressed:

  s3://raw/ec2/describe_instances/region=<region>/dt=2026-06-12/hour=02/describe_instances.parquet

Adapted from local-kubernetes/data-gen/ec2_describe_instances.py; fleet events are
now relative to --start so any 6-month window works.

Each file is the DescribeInstances response as the API returns it: one row per
Reservation (ReservationId, OwnerId, RequesterId, Groups) with the nested
`Instances` list, using the EC2 API's field names. Every file is written with the
same explicit schema.

The fleet is simulated rather than random per file:
  * instances keep their IDs across snapshots and move through
    pending -> running -> stopping/stopped -> shutting-down -> terminated
  * several AWS accounts share each regional file (one account per team)
  * auto-scaling groups follow a daily curve in the region's local time, Spark
    runs at night on spot, CI runners run in office hours, ML boxes are stopped
    and started, spot instances get interrupted
  * a legacy Windows fleet is decommissioned in March and a new service launches
    in ap-southeast-1 in April

Deterministic: the same arguments always produce the same files.
"""
import argparse
import io
import math
import os
import random
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from multiprocessing import Pool

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

# --------------------------------------------------------------------------- schema

_tag = pa.struct([("Key", pa.string()), ("Value", pa.string())])
_group = pa.struct([("GroupId", pa.string()), ("GroupName", pa.string())])
_ebs = pa.struct([
    ("AttachTime", pa.string()), ("DeleteOnTermination", pa.bool_()),
    ("Status", pa.string()), ("VolumeId", pa.string()),
])
_block_device = pa.struct([("DeviceName", pa.string()), ("Ebs", _ebs)])
_association = pa.struct([("IpOwnerId", pa.string()), ("PublicDnsName", pa.string()), ("PublicIp", pa.string())])
_eni_attachment = pa.struct([
    ("AttachTime", pa.string()), ("AttachmentId", pa.string()), ("DeleteOnTermination", pa.bool_()),
    ("DeviceIndex", pa.int64()), ("Status", pa.string()), ("NetworkCardIndex", pa.int64()),
])
_private_ip = pa.struct([
    ("Association", _association), ("Primary", pa.bool_()),
    ("PrivateDnsName", pa.string()), ("PrivateIpAddress", pa.string()),
])
_network_interface = pa.struct([
    ("Association", _association), ("Attachment", _eni_attachment), ("Description", pa.string()),
    ("Groups", pa.list_(_group)), ("Ipv6Addresses", pa.list_(pa.struct([("Ipv6Address", pa.string())]))),
    ("MacAddress", pa.string()), ("NetworkInterfaceId", pa.string()), ("OwnerId", pa.string()),
    ("PrivateDnsName", pa.string()), ("PrivateIpAddress", pa.string()),
    ("PrivateIpAddresses", pa.list_(_private_ip)), ("SourceDestCheck", pa.bool_()),
    ("Status", pa.string()), ("SubnetId", pa.string()), ("VpcId", pa.string()), ("InterfaceType", pa.string()),
])
_instance = pa.struct([
    ("AmiLaunchIndex", pa.int64()),
    ("ImageId", pa.string()),
    ("InstanceId", pa.string()),
    ("InstanceType", pa.string()),
    ("KeyName", pa.string()),
    ("LaunchTime", pa.string()),
    ("Monitoring", pa.struct([("State", pa.string())])),
    ("Placement", pa.struct([("AvailabilityZone", pa.string()), ("GroupName", pa.string()), ("Tenancy", pa.string())])),
    ("Platform", pa.string()),
    ("PrivateDnsName", pa.string()),
    ("PrivateIpAddress", pa.string()),
    ("ProductCodes", pa.list_(pa.struct([("ProductCodeId", pa.string()), ("ProductCodeType", pa.string())]))),
    ("PublicDnsName", pa.string()),
    ("PublicIpAddress", pa.string()),
    ("State", pa.struct([("Code", pa.int64()), ("Name", pa.string())])),
    ("StateTransitionReason", pa.string()),
    ("SubnetId", pa.string()),
    ("VpcId", pa.string()),
    ("Architecture", pa.string()),
    ("BlockDeviceMappings", pa.list_(_block_device)),
    ("ClientToken", pa.string()),
    ("EbsOptimized", pa.bool_()),
    ("EnaSupport", pa.bool_()),
    ("Hypervisor", pa.string()),
    ("IamInstanceProfile", pa.struct([("Arn", pa.string()), ("Id", pa.string())])),
    ("InstanceLifecycle", pa.string()),
    ("SpotInstanceRequestId", pa.string()),
    ("NetworkInterfaces", pa.list_(_network_interface)),
    ("RootDeviceName", pa.string()),
    ("RootDeviceType", pa.string()),
    ("SecurityGroups", pa.list_(_group)),
    ("SourceDestCheck", pa.bool_()),
    ("StateReason", pa.struct([("Code", pa.string()), ("Message", pa.string())])),
    ("Tags", pa.list_(_tag)),
    ("VirtualizationType", pa.string()),
    ("CpuOptions", pa.struct([("CoreCount", pa.int64()), ("ThreadsPerCore", pa.int64())])),
    ("CapacityReservationSpecification", pa.struct([("CapacityReservationPreference", pa.string())])),
    ("HibernationOptions", pa.struct([("Configured", pa.bool_())])),
    ("MetadataOptions", pa.struct([
        ("State", pa.string()), ("HttpTokens", pa.string()), ("HttpPutResponseHopLimit", pa.int64()),
        ("HttpEndpoint", pa.string()), ("HttpProtocolIpv6", pa.string()), ("InstanceMetadataTags", pa.string()),
    ])),
    ("EnclaveOptions", pa.struct([("Enabled", pa.bool_())])),
    ("BootMode", pa.string()),
    ("PlatformDetails", pa.string()),
    ("UsageOperation", pa.string()),
    ("UsageOperationUpdateTime", pa.string()),
    ("PrivateDnsNameOptions", pa.struct([
        ("HostnameType", pa.string()), ("EnableResourceNameDnsARecord", pa.bool_()),
        ("EnableResourceNameDnsAAAARecord", pa.bool_()),
    ])),
    ("MaintenanceOptions", pa.struct([("AutoRecovery", pa.string())])),
    ("CurrentInstanceBootMode", pa.string()),
])
SCHEMA = pa.schema([
    ("Groups", pa.list_(_group)),
    ("Instances", pa.list_(_instance)),
    ("OwnerId", pa.string()),
    ("RequesterId", pa.string()),
    ("ReservationId", pa.string()),
])

# ------------------------------------------------------------------------ the fleet

REGIONS = {
    # scale: fleet size relative to eu-central-1; utc_offset drives the daily curves
    "us-east-1":      dict(scale=1.5,  utc_offset=-5, azs="abcdf", octet=10),
    "eu-central-1":   dict(scale=1.0,  utc_offset=1,  azs="abc",   octet=20),
    "ap-northeast-1": dict(scale=0.6,  utc_offset=9,  azs="acd",   octet=30),
    "ap-southeast-1": dict(scale=0.35, utc_offset=8,  azs="abc",   octet=40),
}

# One AWS account per team (fake 12-digit IDs).
ACCOUNTS = {
    "platform":  "392918273645",
    "payments":  "345678901234",
    "search":    "456789012345",
    "data":      "567890123456",
    "ml":        "678901234567",
    "devex":     "789012345678",
    "finance":   "890123456789",
    "logistics": "901234567890",
}
ASG_REQUESTER = "999999999999"  # stand-in for the AWS-owned requester of ASG launches

STATE_CODES = {"pending": 0, "running": 16, "shutting-down": 32, "terminated": 48, "stopping": 64, "stopped": 80}


@dataclass
class Service:
    name: str
    team: str
    types: list            # [(instance_type, weight)]
    base: int              # instance count in a scale-1.0 region
    pattern: str           # steady | diurnal | night_batch | office_hours | stop_start | decommission | new_launch
    asg: bool = False
    spot: float = 0.0      # share of launches that are spot
    public: bool = False
    windows: float = 0.0   # share of Windows instances
    regions: tuple = ()    # empty = every region
    data_volume: bool = False
    amplitude: float = 0.4
    launch_after_days: int = 0  # new_launch: days after the start of the data


SERVICES = [
    Service("eks-general", "platform", [("m6i.2xlarge", 5), ("m7i.2xlarge", 3), ("m6i.4xlarge", 2)], 60, "diurnal",
            asg=True, amplitude=0.35),
    Service("checkout-api", "payments", [("m7i.large", 6), ("m7i.xlarge", 4)], 40, "diurnal", asg=True, amplitude=0.6),
    Service("payments-db-proxy", "payments", [("r6i.large", 1)], 8, "steady"),
    Service("search-indexer", "search", [("r7g.2xlarge", 3), ("r7g.4xlarge", 1)], 24, "steady", data_volume=True),
    Service("opensearch-data", "search", [("r6g.4xlarge", 2), ("i4g.4xlarge", 1)], 18, "steady", data_volume=True),
    Service("kafka-broker", "platform", [("i4i.2xlarge", 1)], 12, "steady", data_volume=True),
    Service("bastion", "platform", [("t3.micro", 1)], 2, "steady", public=True),
    Service("spark-batch", "data", [("c7g.4xlarge", 3), ("r7g.4xlarge", 2)], 50, "night_batch", asg=True, spot=0.7),
    Service("airflow-worker", "data", [("m6g.xlarge", 1)], 10, "steady"),
    Service("ci-runner", "devex", [("c7a.2xlarge", 3), ("c7a.4xlarge", 1)], 45, "office_hours", asg=True, spot=0.8,
            regions=("us-east-1", "eu-central-1")),
    Service("ml-training", "ml", [("g5.2xlarge", 6), ("g5.12xlarge", 3), ("p4d.24xlarge", 1)], 16, "stop_start",
            regions=("us-east-1", "eu-central-1"), data_volume=True),
    Service("jupyter", "ml", [("t3.xlarge", 2), ("m6i.xlarge", 1)], 20, "stop_start",
            regions=("us-east-1", "eu-central-1")),
    Service("legacy-reporting", "finance", [("m5.large", 2), ("m5.xlarge", 1)], 14, "decommission", windows=0.6,
            regions=("us-east-1", "eu-central-1")),
    Service("fulfillment-edge", "logistics", [("c7i.xlarge", 3), ("c7i.2xlarge", 1)], 30, "new_launch", asg=True,
            public=True, regions=("ap-southeast-1",), launch_after_days=65, amplitude=0.5),
]

VCPUS = {"micro": 2, "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "12xlarge": 48, "24xlarge": 96}


def is_arm(instance_type):
    family = instance_type.split(".")[0]
    return family[-1] == "g" or family.endswith("gd")


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@dataclass
class Instance:
    id: str
    reservation_id: str
    owner: str
    requester: str
    service: Service
    region: str
    type: str
    az: str
    launch_time: datetime
    private_ip: str
    subnet: str
    vpc: str
    ami: str
    lifecycle: str          # "" (on-demand) or "spot"
    windows: bool
    ami_launch_index: int
    client_token: str
    cost_center_tag: bool
    rng_bits: dict = field(default_factory=dict)
    state: str = "running"
    public_ip: str = None
    transition_reason: str = ""
    state_reason: tuple = None
    stopped_since: datetime = None
    _cache: dict = None


class Fleet:
    """All instances of one region, advanced one 2-hour snapshot at a time."""

    def __init__(self, region, start):
        self.region = region
        self.cfg = REGIONS[region]
        self.rng = random.Random(f"fleet-{region}")
        self.start = start
        self.instances = {}          # InstanceId -> Instance, insertion-ordered
        self.pools = {}              # service name -> the stable population for stop_start/decommission
        self.vpc = self._id("vpc")
        self.subnets = {az: self._id("subnet") for az in self.cfg["azs"]}
        self.amis = {s.name: (self._id("ami"), self._id("ami")) for s in SERVICES}
        self.sgs = {s.name: [(self._id("sg"), f"{s.name}-prd"), (self._id("sg"), "default-egress")] for s in SERVICES}
        self.services = [s for s in SERVICES if not s.regions or region in s.regions]
        # Initial fleet: everything the targets ask for at t0 is already running.
        for svc in self.services:
            pooled = svc.pattern in ("stop_start", "decommission")
            self.launch(svc, self.pool_size(svc) if pooled else self.target(svc, start), start, initial=True)
            if pooled:
                self.pools[svc.name] = [i.id for i in self.instances.values() if i.service is svc]

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix):
        return f"{prefix}-{self.rng.getrandbits(68):017x}"

    def local(self, ts):
        return ts + timedelta(hours=self.cfg["utc_offset"])

    def growth(self, ts):
        return 1 + 0.02 * (ts - self.start).days / 30

    def pool_size(self, svc):
        return max(1, round(svc.base * self.cfg["scale"]))

    def target(self, svc, ts):
        local = self.local(ts)
        base = svc.base * self.cfg["scale"] * self.growth(ts)
        weekend = local.weekday() >= 5
        if svc.pattern == "steady":
            return max(1, round(base))
        if svc.pattern == "diurnal":
            curve = 1 + svc.amplitude * math.cos(2 * math.pi * (local.hour - 14) / 24)
            return max(2, round(base * curve * (0.8 if weekend else 1.0)))
        if svc.pattern == "night_batch":
            return round(base if 0 <= local.hour < 6 else base * 0.15)
        if svc.pattern == "office_hours":
            return round(base if (not weekend and 8 <= local.hour < 20) else base * 0.05)
        if svc.pattern == "new_launch":
            launch_date = self.start.date() + timedelta(days=svc.launch_after_days)
            if ts.date() < launch_date:
                return 0
            ramp = min(1.0, 0.15 + (ts.date() - launch_date).days / 21)
            curve = 1 + svc.amplitude * math.cos(2 * math.pi * (local.hour - 14) / 24)
            return max(2, round(svc.base * ramp * curve))
        raise ValueError(svc.pattern)

    def launch(self, svc, count, ts, initial=False):
        if count <= 0:
            return
        rng = self.rng
        reservation, owner = self._id("r"), ACCOUNTS[svc.team]
        for n in range(count):
            if n and n % rng.choice((1, 2, 4, 8)) == 0:
                reservation = self._id("r")   # launches are grouped into reservations of varying size
            az = rng.choice(self.cfg["azs"])
            itype = rng.choices([t for t, _ in svc.types], [w for _, w in svc.types])[0]
            if initial:
                launched = ts - timedelta(days=rng.uniform(0.1, 1 if svc.asg else 300))
            else:
                launched = ts - timedelta(minutes=rng.uniform(0.5, 118))
            inst = Instance(
                id=self._id("i"), reservation_id=reservation, owner=owner,
                requester=ASG_REQUESTER if svc.asg else None, service=svc, region=self.region, type=itype,
                az=f"{self.region}{az}", launch_time=launched,
                private_ip=f"10.{self.cfg['octet']}.{rng.randint(0, 63)}.{rng.randint(4, 254)}",
                subnet=self.subnets[az], vpc=self.vpc, ami=rng.choice(self.amis[svc.name]),
                lifecycle="spot" if rng.random() < svc.spot else "",
                windows=rng.random() < svc.windows, ami_launch_index=n,
                client_token=f"{rng.getrandbits(128):032x}", cost_center_tag=rng.random() > 0.06,
                rng_bits={"mac": rng.getrandbits(40), "eni": self._id("eni"), "vol": self._id("vol"),
                          "vol2": self._id("vol"), "attach": self._id("eni-attach"), "sir": self._id("sir")},
            )
            if svc.public:
                inst.public_ip = self.public_ip()
            if not initial and rng.random() < 0.2:
                inst.state = "pending"
                inst.launch_time = ts - timedelta(minutes=rng.uniform(0.2, 3))
            self.instances[inst.id] = inst

    def public_ip(self):
        first = {"us-east-1": 3, "eu-central-1": 18, "ap-northeast-1": 13, "ap-southeast-1": 54}[self.region]
        return f"{first}.{self.rng.randint(0, 255)}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}"

    def set_state(self, inst, state, ts=None, reason=None):
        inst.state = state
        inst._cache = None
        if state in ("stopping", "stopped"):
            inst.transition_reason = f"User initiated ({ts:%Y-%m-%d %H:%M:%S} GMT)"
            inst.state_reason = ("Client.UserInitiatedShutdown", "Client.UserInitiatedShutdown: User initiated shutdown")
            inst.public_ip = None
            inst.stopped_since = inst.stopped_since or ts
        elif state in ("shutting-down", "terminated"):
            code = reason or "Client.UserInitiatedShutdown"
            message = {"Server.SpotInstanceTermination": "Server.SpotInstanceTermination: Spot instance termination",
                       "Client.UserInitiatedShutdown": "Client.UserInitiatedShutdown: User initiated shutdown"}[code]
            inst.transition_reason = f"User initiated ({ts:%Y-%m-%d %H:%M:%S} GMT)"
            inst.state_reason = (code, message)
            inst.public_ip = None
        elif state in ("pending", "running"):
            inst.transition_reason, inst.state_reason, inst.stopped_since = "", None, None
            if inst.service.public and not inst.public_ip:
                inst.public_ip = self.public_ip()

    def terminate(self, inst, ts, reason=None):
        self.set_state(inst, "shutting-down" if self.rng.random() < 0.2 else "terminated", ts, reason)

    # ------------------------------------------------------------------ one step
    def step(self, ts):
        rng = self.rng
        # 1. finish transitions from the previous snapshot
        for inst in list(self.instances.values()):
            if inst.state == "terminated":
                del self.instances[inst.id]
            elif inst.state == "shutting-down":
                self.set_state(inst, "terminated", ts, inst.state_reason[0])
            elif inst.state == "pending":
                self.set_state(inst, "running", ts)
            elif inst.state == "stopping":
                inst.state, inst._cache = "stopped", None

        for svc in self.services:
            members = [i for i in self.instances.values() if i.service is svc]
            active = [i for i in members if i.state in ("pending", "running")]

            if svc.pattern in ("stop_start", "decommission"):
                self.step_pool(svc, members, active, ts)
                continue

            # 2. random events: spot interruptions and hardware retirement
            for inst in active:
                if inst.lifecycle == "spot" and rng.random() < 0.004:
                    self.terminate(inst, ts, "Server.SpotInstanceTermination")
                elif rng.random() < 0.0004:
                    self.terminate(inst, ts)
            active = [i for i in active if i.state in ("pending", "running")]

            # 3. reconcile to the target like an ASG / Terraform would
            target = self.target(svc, ts)
            if len(active) < target:
                self.launch(svc, target - len(active), ts)
            elif len(active) > target:
                for inst in rng.sample(active, len(active) - target):
                    self.terminate(inst, ts)

    def step_pool(self, svc, members, active, ts):
        rng = self.rng
        local = self.local(ts)
        stopped = [i for i in members if i.state == "stopped"]
        if svc.pattern == "stop_start":
            weekend = local.weekday() >= 5
            share = 0.1 if weekend else (0.7 if 8 <= local.hour < 20 else 0.25)
            want = round(len(members) * share)
            if len(active) < want and stopped:
                for inst in rng.sample(stopped, min(len(stopped), want - len(active))):
                    self.set_state(inst, "pending" if rng.random() < 0.15 else "running", ts)
                    inst.launch_time = ts - timedelta(minutes=rng.uniform(0.5, 118))
            elif len(active) > want:
                for inst in rng.sample(active, len(active) - want):
                    self.set_state(inst, "stopping" if rng.random() < 0.1 else "stopped", ts)
            # boxes stopped for more than 3 weeks get cleaned up and replaced
            # (re-check state: some of `stopped` were just started above)
            for inst in stopped:
                if inst.state == "stopped" and ts - inst.stopped_since > timedelta(days=21) and rng.random() < 0.05:
                    self.terminate(inst, ts)
                    self.launch(svc, 1, ts)
                    self.set_state(list(self.instances.values())[-1], "stopped", ts)
        else:  # decommission: terminated gradually between day 60 and day 105 of the data
            start, end = self.start + timedelta(days=60), self.start + timedelta(days=105)
            keep = len(self.pools[svc.name])
            if ts >= start:
                keep = max(0, round(keep * (1 - min(1.0, (ts - start) / (end - start)))))
            alive = [i for i in members if i.state not in ("shutting-down", "terminated")]
            for inst in rng.sample(alive, max(0, len(alive) - keep)):
                self.terminate(inst, ts)
            for inst in alive:
                if inst.state in ("running", "stopped") and rng.random() < 0.01:
                    if inst.state == "running":
                        self.set_state(inst, "stopped", ts)
                    else:
                        self.set_state(inst, "running", ts)

    # ------------------------------------------------------------------ rendering
    def render(self, inst):
        if inst._cache is not None:
            return inst._cache
        svc, bits, region = inst.service, inst.rng_bits, self.region
        state = inst.state
        gone = state == "terminated"
        dns_suffix = "ec2.internal" if region == "us-east-1" else f"{region}.compute.internal"
        private_dns = "" if gone else f"ip-{inst.private_ip.replace('.', '-')}.{dns_suffix}"
        public_dns = ""
        association = None
        if inst.public_ip and not gone:
            host = "compute-1" if region == "us-east-1" else f"{region}.compute"
            public_dns = f"ec2-{inst.public_ip.replace('.', '-')}.{host}.amazonaws.com"
            association = {"IpOwnerId": "amazon", "PublicDnsName": public_dns, "PublicIp": inst.public_ip}
        size = inst.type.split(".")[1]
        vcpu = VCPUS.get(size, 4)
        arm = is_arm(inst.type)
        cores, threads = (vcpu, 1) if arm else (max(1, vcpu // 2), 2)
        launch = iso(inst.launch_time)
        tags = [
            {"Key": "Name", "Value": svc.name if svc.asg else f"{svc.name}-prd-{inst.az[-1]}"},
            {"Key": "Environment", "Value": "prd"},
            {"Key": "Team", "Value": svc.team},
            {"Key": "Service", "Value": svc.name},
        ]
        if inst.cost_center_tag:
            tags.append({"Key": "CostCenter", "Value": f"cc-{zlib.crc32(svc.team.encode()) % 9000 + 1000}"})
        if svc.asg:
            tags.append({"Key": "aws:autoscaling:groupName", "Value": f"{svc.name}-prd-asg"})
        if svc.name.startswith("eks"):
            tags += [{"Key": "eks:cluster-name", "Value": "prd-main"},
                     {"Key": "eks:nodegroup-name", "Value": "general"},
                     {"Key": "kubernetes.io/cluster/prd-main", "Value": "owned"}]
        else:
            tags.append({"Key": "ManagedBy", "Value": "terraform"})

        block_devices, interfaces, groups = [], [], []
        if not gone:
            block_devices.append({"DeviceName": "/dev/xvda", "Ebs": {
                "AttachTime": launch, "DeleteOnTermination": True, "Status": "attached", "VolumeId": bits["vol"]}})
            if svc.data_volume:
                block_devices.append({"DeviceName": "/dev/sdb", "Ebs": {
                    "AttachTime": launch, "DeleteOnTermination": False, "Status": "attached", "VolumeId": bits["vol2"]}})
            groups = [{"GroupId": gid, "GroupName": name} for gid, name in self.sgs[svc.name]]
            mac = ":".join(f"{(bits['mac'] >> (8 * k)) & 0xFF:02x}" for k in range(5))
            interfaces.append({
                "Association": association,
                "Attachment": {"AttachTime": launch, "AttachmentId": bits["attach"], "DeleteOnTermination": True,
                               "DeviceIndex": 0, "Status": "attached", "NetworkCardIndex": 0},
                "Description": "", "Groups": groups, "Ipv6Addresses": [], "MacAddress": f"06:{mac}",
                "NetworkInterfaceId": bits["eni"], "OwnerId": inst.owner, "PrivateDnsName": private_dns,
                "PrivateIpAddress": inst.private_ip,
                "PrivateIpAddresses": [{"Association": association, "Primary": True,
                                        "PrivateDnsName": private_dns, "PrivateIpAddress": inst.private_ip}],
                "SourceDestCheck": True, "Status": "in-use", "SubnetId": inst.subnet, "VpcId": inst.vpc,
                "InterfaceType": "interface",
            })

        doc = {
            "AmiLaunchIndex": inst.ami_launch_index,
            "ImageId": inst.ami,
            "InstanceId": inst.id,
            "InstanceType": inst.type,
            "KeyName": None if svc.asg else f"{svc.team}-prd",
            "LaunchTime": launch,
            "Monitoring": {"State": "enabled" if svc.data_volume else "disabled"},
            "Placement": {"AvailabilityZone": inst.az, "GroupName": "", "Tenancy": "default"},
            "Platform": "windows" if inst.windows else None,
            "PrivateDnsName": private_dns,
            "PrivateIpAddress": None if gone else inst.private_ip,
            "ProductCodes": [],
            "PublicDnsName": public_dns,
            "PublicIpAddress": None if gone else inst.public_ip,
            "State": {"Code": STATE_CODES[state], "Name": state},
            "StateTransitionReason": inst.transition_reason,
            "SubnetId": None if gone else inst.subnet,
            "VpcId": None if gone else inst.vpc,
            "Architecture": "arm64" if arm else "x86_64",
            "BlockDeviceMappings": block_devices,
            "ClientToken": inst.client_token,
            "EbsOptimized": True,
            "EnaSupport": True,
            "Hypervisor": "xen",
            "IamInstanceProfile": {
                "Arn": f"arn:aws:iam::{inst.owner}:instance-profile/{svc.name}-prd",
                "Id": f"AIPA{zlib.crc32(f'{svc.name}/{inst.owner}'.encode()):016d}",
            },
            "InstanceLifecycle": inst.lifecycle or None,
            "SpotInstanceRequestId": bits["sir"] if inst.lifecycle else None,
            "NetworkInterfaces": interfaces,
            "RootDeviceName": "/dev/xvda",
            "RootDeviceType": "ebs",
            "SecurityGroups": groups,
            "SourceDestCheck": True,
            "StateReason": ({"Code": inst.state_reason[0], "Message": inst.state_reason[1]}
                            if inst.state_reason else None),
            "Tags": tags,
            "VirtualizationType": "hvm",
            "CpuOptions": {"CoreCount": cores, "ThreadsPerCore": threads},
            "CapacityReservationSpecification": {"CapacityReservationPreference": "open"},
            "HibernationOptions": {"Configured": False},
            "MetadataOptions": {"State": "applied", "HttpTokens": "required", "HttpPutResponseHopLimit": 2,
                                "HttpEndpoint": "enabled", "HttpProtocolIpv6": "disabled",
                                "InstanceMetadataTags": "disabled"},
            "EnclaveOptions": {"Enabled": False},
            "BootMode": "uefi" if arm else "uefi-preferred",
            "PlatformDetails": "Windows" if inst.windows else "Linux/UNIX",
            "UsageOperation": "RunInstances:0002" if inst.windows else "RunInstances",
            "UsageOperationUpdateTime": launch,
            "PrivateDnsNameOptions": {"HostnameType": "ip-name", "EnableResourceNameDnsARecord": False,
                                      "EnableResourceNameDnsAAAARecord": False},
            "MaintenanceOptions": {"AutoRecovery": "default"},
            "CurrentInstanceBootMode": "uefi" if arm else "legacy-bios",
        }
        inst._cache = doc
        return doc

    def snapshot(self):
        reservations = {}
        for inst in self.instances.values():
            res = reservations.get(inst.reservation_id)
            if res is None:
                res = reservations[inst.reservation_id] = {
                    "Groups": [], "Instances": [], "OwnerId": inst.owner,
                    "RequesterId": inst.requester, "ReservationId": inst.reservation_id,
                }
            res["Instances"].append(self.render(inst))
        return pa.Table.from_pylist(list(reservations.values()), schema=SCHEMA)


# -------------------------------------------------------------------------- output

def with_retries(fn, what, attempts=6):
    """Run fn(), retrying S3 errors with backoff.

    Under load the object store occasionally drops a connection
    (AWS Error NETWORK_CONNECTION); a retry succeeds.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as err:
            if attempt == attempts - 1:
                raise
            wait = 2 ** attempt
            print(f"retrying {what} in {wait}s: {err}", flush=True)
            time.sleep(wait)


def make_fs(args):
    if args.output_dir:
        return pafs.LocalFileSystem(), args.output_dir.rstrip("/")
    # The AWS SDK defaults (1 s connect / 3 s request timeout, 3 attempts) are too
    # tight for a single-node object store that is busy with other loads.
    fs = pafs.S3FileSystem(access_key=args.access_key, secret_key=args.secret_key, region="us-east-1",
                           endpoint_override=args.endpoint, scheme="http",
                           connect_timeout=10, request_timeout=120,
                           retry_strategy=pafs.AwsStandardS3RetryStrategy(max_attempts=10))
    return fs, args.bucket


def object_path(root, prefix, region, ts):
    return f"{root}/{prefix}/region={region}/dt={ts:%Y-%m-%d}/hour={ts:%H}/describe_instances.parquet"


def generate_region(job):
    region, args = job
    fs, root = make_fs(args)
    start = datetime.combine(args.start, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(args.end, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    fleet = Fleet(region, start)
    files = 0
    t0 = time.time()
    ts = start
    while ts < end:
        if ts > start:
            fleet.step(ts)
        table = fleet.snapshot()
        path = object_path(root, args.prefix, region, ts)
        if args.output_dir:
            fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd", compression_level=3)
        data = buf.getvalue()

        def upload():
            with fs.open_output_stream(path) as out:
                out.write(data)

        with_retries(upload, f"upload {path}")
        files += 1
        if ts.hour == 22 and ts.day == 1:
            print(f"[{region}] {ts:%Y-%m-%d} files={files} fleet={len(fleet.instances)} "
                  f"elapsed={time.time() - t0:.0f}s", flush=True)
        ts += timedelta(hours=2)
    print(f"[{region}] done: {files} files, final fleet {len(fleet.instances)} instances, "
          f"{time.time() - t0:.0f}s", flush=True)
    return files


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 4, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 30), help="inclusive")
    p.add_argument("--regions", default=",".join(REGIONS))
    p.add_argument("--bucket", default="raw")
    p.add_argument("--prefix", default="ec2/describe_instances")
    p.add_argument("--endpoint", default=os.environ.get("S3_ENDPOINT_HOSTPORT", "seaweedfs:8333"))
    p.add_argument("--access-key", default=os.environ.get("S3_ACCESS_KEY", "s3admin"))
    p.add_argument("--secret-key", default=os.environ.get("S3_SECRET_KEY", "s3admin-local-secret"))
    p.add_argument("--output-dir", help="write to a local directory instead of S3")
    p.add_argument("--force", action="store_true", help="regenerate even if the last file already exists")
    p.add_argument("--processes", type=int, default=4, help="parallel writer processes (one region each)")
    args = p.parse_args()

    regions = args.regions.split(",")
    fs, root = make_fs(args)
    last = datetime.combine(args.end, datetime.min.time(), tzinfo=UTC) + timedelta(hours=22)

    def already_present():
        return all(fs.get_file_info(object_path(root, args.prefix, r, last)).type == pafs.FileType.File
                   for r in regions)

    if not args.force and with_retries(already_present, "existence check"):
        print("data already present (last snapshot exists for every region); use --force to regenerate")
        return
    with Pool(min(args.processes, len(regions))) as pool:
        total = sum(pool.map(generate_region, [(r, args) for r in regions]))
    print(f"wrote {total} files")


if __name__ == "__main__":
    sys.exit(main())
