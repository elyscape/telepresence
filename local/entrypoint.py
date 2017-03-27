#!/usr/bin/python3
"""
THIS IS A PROTOTYPE.

As a result the code is quite awful. Next up is rewriting it with tests and
abstractions.
"""

from json import loads
from os import setuid, rename
from subprocess import Popen, check_output, check_call, CalledProcessError
from sys import argv, exit
import time


def _get_service_names(environment):
    """Return names of Services, as used in env variable names."""
    # XXX duplicated in remote-telepresence
    # XXX also check for TCPness.
    # Order matters for service_keys, need it to be consistent with port
    # forwarding order in remote container.
    result = [
        key[:-len("_SERVICE_HOST")] for key in environment
        if key.endswith("_SERVICE_HOST")
    ]
    result.sort()
    return result


def get_remote_env(pod_name):
    """Get the environment variables in the remote pod."""
    env = str(check_output(["kubectl", "exec", pod_name, "env"]), "utf-8")
    result = {}
    for line in env.splitlines():
        key, value = line.split("=", 1)
        result[key] = value
    return result


def get_deployment_set_keys(deployment_name):
    """Get the set of environment variables names set by the Deployment."""
    deployment = loads(
        str(
            check_output([
                "kubectl", "get", "deployment", "-o", "json", deployment_name
            ]), "utf-8"
        )
    )
    container = [
        c for c in deployment["spec"]["template"]["spec"]["containers"]
        if "telepresence-k8s" in c["image"]
    ][0]
    return set([var["name"] for var in container.get("env", [])])


def get_env_variables(pod_name, deployment_name):
    """
    Generate environment variables that match kubernetes.

    For both Docker and non-Docker we copy environment variables explicitly set
    in the Deployment template.

    For Docker we make modified versions of the Servic env variables. For
    non-Docker (SOCKS) we just copy the Service env variables as is.
    """
    remote_env = get_remote_env(pod_name)
    deployment_set_keys = get_deployment_set_keys(deployment_name)
    service_names = _get_service_names(remote_env)
    # ips proxied via docker, so need to modify addresses:
    in_docker_result = {}
    # XXX we're recreating the port generation logic
    i = 0
    for i, name in enumerate(service_names):
        port = str(2000 + i)
        ip = "127.0.0.1"
        # XXX will be wrong for UDP
        full_address = "tcp://{}:{}".format(ip, port)
        in_docker_result[name + "_SERVICE_HOST"] = ip
        in_docker_result[name + "_SERVICE_PORT"] = port
        in_docker_result[name + "_PORT"] = full_address
        port_name = name + "_PORT_" + port + "_TCP"
        in_docker_result[port_name] = full_address
        # XXX will break for UDP
        in_docker_result[port_name + "_PROTO"] = "tcp"
        in_docker_result[port_name + "_PORT"] = port
        in_docker_result[port_name + "_ADDR"] = ip
    socks_result = {}
    for key, value in remote_env.items():
        if key in deployment_set_keys:
            # Copy over Deployment-set env variables:
            in_docker_result[key] = value
            socks_result[key] = value
        for service_name in service_names:
            # Copy over Service env variables to SOCKS variant:
            if key.startswith(service_name + "_") and (
                key.endswith("_ADDR") or key.endswith("_PORT") or
                key.endswith("_PROTO") or key.endswith("_HOST") or
                key.endswith("_TCP")
            ):
                socks_result[key] = value
    return in_docker_result, socks_result


def write_env(pod_name, deployment_name):
    for_docker_env, for_local_env = get_env_variables(
        pod_name, deployment_name
    )
    with open("/output/unproxied.env.tmp", "w") as f:
        for key, value in for_local_env.items():
            f.write("{}={}\n".format(key, value))
    rename("/output/unproxied.env.tmp", "/output/unproxied.env")
    with open("/output/docker.env.tmp", "w") as f:
        for key, value in for_docker_env.items():
            f.write("{}={}\n".format(key, value))
    rename("/output/docker.env.tmp", "/output/docker.env")


def write_etc_hosts(additional_hosts):
    """
    Update /etc/hosts with records that match k8s DNS entries for services.
    """
    services_json = loads(
        str(
            check_output(["kubectl", "get", "service", "-o", "json"]), "utf-8"
        )
    )
    with open("/etc/hosts", "a") as hosts:
        for service in services_json["items"]:
            name = service["metadata"]["name"]
            namespace = service["metadata"]["namespace"]
            hosts.write("127.0.0.1 {}\n".format(name))
            hosts.write(
                "127.0.0.1 {}.{}.svc.cluster.local\n".format(name, namespace)
            )
        for host in additional_hosts:
            hosts.write("127.0.0.1 {}\n".format(host))


def get_pod_name(deployment_name):
    """Given the deployment name, return the name of its pod."""
    deployment = loads(
        str(
            check_output([
                "kubectl",
                "get",
                "deployment",
                "-o",
                "json",
                deployment_name,
                "--export",
            ]), "utf-8"
        )
    )
    expected_metadata = deployment["spec"]["template"]["metadata"]
    print("Expected metadata for pods: {}".format(expected_metadata))
    pods = loads(
        str(
            check_output(["kubectl", "get", "pod", "-o", "json", "--export"]),
            "utf-8"
        )
    )["items"]

    for pod in pods:
        name = pod["metadata"]["name"]
        phase = pod["status"]["phase"]
        print(
            "Checking {} (phase {})...".
            format(pod["metadata"].get("labels"), phase)
        )
        if not set(expected_metadata.get("labels", {}).items()
                   ).issubset(set(pod["metadata"].get("labels", {}).items())):
            print("Labels don't match.")
            continue
        if (name.startswith(deployment_name + "-")
            and
            pod["metadata"]["namespace"] == deployment["metadata"].get(
                "namespace", "default")
            and
            phase in (
                "Pending", "Running"
        )):
            print("Looks like we've found our pod!")
            return name
    raise RuntimeError(
        "Telepresence pod not found for Deployment '{}'.".
        format(deployment_name)
    )


def ssh(args):
    """Connect to remote pod via SSH.

    Returns Popen object.
    """
    return Popen([
        "sshpass", "-phello", "ssh", "-q", "-oStrictHostKeyChecking=no",
        "root@localhost", "-N"
    ] + args)


def wait_for_ssh():
    for i in range(30):
        try:
            check_call([
                "sshpass", "-phello", "ssh", "-q",
                "-oStrictHostKeyChecking=no", "root@localhost", "/bin/true"
            ])
        except CalledProcessError:
            time.sleep(1)
        else:
            return
    raise RuntimeError("SSH isn't starting.")


def wait_for_pod(pod_name):
    for i in range(120):
        phase = str(
            check_output([
                "kubectl", "get", "pod", pod_name, "-o",
                "jsonpath={.status.phase}"
            ]), "utf-8"
        ).strip()
        if phase == "Running":
            return
        time.sleep(1)
    raise RuntimeError("Pod isn't starting: {}".format(phase))


SOCKS_PORT = 9050


def main(
    uid, deployment_name, local_exposed_ports, custom_proxied_hosts,
    expose_host
):
    processes = []
    pod_name = get_pod_name(deployment_name)
    # Wait for pod to be running:
    wait_for_pod(pod_name)
    proxied_ports = set(range(2000, 2020)) | set(map(int, local_exposed_ports))
    proxied_ports.add(22)
    proxied_ports.add(SOCKS_PORT)
    custom_ports = [int(s.split(":", 1)[1]) for s in custom_proxied_hosts]
    for port in custom_ports:
        if port in proxied_ports:
            exit((
                "OOPS: Can't proxy port {} more than once. "
                "Currently mapped ports: {}.This error is due "
                "to a limitation in Telepresence, see "
                "https://github.com/datawire/telepresence/issues/6"
            ).format(port, proxied_ports))
        else:
            proxied_ports.add(int(port))

    # write /etc/hosts
    write_etc_hosts([s.split(":", 1)[0] for s in custom_proxied_hosts])

    # forward remote port to here, by tunneling via remote SSH server:
    processes.append(Popen(["kubectl", "port-forward", pod_name, "22"]))
    wait_for_ssh()

    for port_number in local_exposed_ports:
        processes.append(
            ssh([
                "-R",
                "*:{}:{}:{}".format(port_number, expose_host, port_number)
            ])
        )

    # start tunnel to remote SOCKS proxy, for telepresence --run:
    processes.append(
        ssh(["-L", "*:{}:127.0.0.1:{}".format(SOCKS_PORT, SOCKS_PORT)])
    )

    # start proxies for custom-mapped hosts:
    for host, port in [s.split(":", 1) for s in custom_proxied_hosts]:
        processes.append(ssh(["-L", "{}:{}:{}".format(port, host, port)]))

    # start proxies for Services:
    # XXX maybe just do everything via SSH, now that we have it?
    for port in range(2000, 2020):
        # XXX what if there is more than 20 services
        p = Popen(["kubectl", "port-forward", pod_name, str(port)])
        processes.append(p)
    time.sleep(5)
    #
    # write docker envfile, which tells CLI we're ready:
    setuid(uid)
    write_env(pod_name, deployment_name)
    for p in processes:
        p.wait()


if __name__ == '__main__':
    main(
        int(argv[1]), argv[2], argv[3].split(",")
        if argv[3] else [], argv[4].split(",") if argv[4] else [], argv[5]
    )
