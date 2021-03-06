#!/usr/bin/python
"""
Copyright 2019 The Ceph-CSI Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.


#pylint: disable=line-too-long
python tool to trace backend image name from pvc
sample input:
python -c oc -k /home/.kube/config -n default -rn rook-ceph -id admin -key
adminkey

Sample output:

+----------+------------------------------------------+----------------------------------------------+-----------------+--------------+------------------+
| PVC Name |                 PV Name                  |                  Image
Name                  | PV name in omap | Image ID in omap | Image in cluster |
+----------+------------------------------------------+----------------------------------------------+-----------------+--------------+------------------+
| rbd-pvc  | pvc-f1a501dd-03f6-45c9-89f4-85eed7a13ef2 | csi-vol-1b00f5f8-b1c1-11e9-8421-9243c1f659f0 |       True      |     True     |      False       |
| rbd-pvcq | pvc-09a8bceb-0f60-4036-85b9-dc89912ae372 | csi-vol-b781b9b1-b1c5-11e9-8421-9243c1f659f0 |       True      |     True     |       True       |
+----------+------------------------------------------+----------------------------------------------+-----------------+--------------+------------------+
"""

import argparse
import subprocess
import json
import sys
import re
import prettytable
PARSER = argparse.ArgumentParser()

# -p pvc-test -k /home/.kube/config -n default -rn rook-ceph
PARSER.add_argument("-p", "--pvcname", default="", help="PVC name")
PARSER.add_argument("-c", "--command", default="oc",
                    help="kubectl or oc command")
PARSER.add_argument("-k", "--kubeconfig", default="",
                    help="kubernetes configuration")
PARSER.add_argument("-n", "--namespace", default="default",
                    help="namespace in which pvc created")
PARSER.add_argument("-t", "--toolboxdeployed", type=bool, default=True,
                    help="is rook toolbox deployed")
PARSER.add_argument("-d", "--debug", type=bool, default=False,
                    help="log commands output")
PARSER.add_argument("-rn", "--rooknamespace",
                    default="rook-ceph", help="rook namespace")
PARSER.add_argument("-id", "--userid",
                    default="admin", help="user ID to connect to ceph cluster")
PARSER.add_argument("-key", "--userkey",
                    default="", help="user password to connect to ceph cluster")


def list_pvc_vol_name_mapping(arg):
    """
    list pvc and volume name mapping
    """
    table = prettytable.PrettyTable(
        ["PVC Name", "PV Name", "Image Name", "PV name in omap",
         "Image ID in omap", "Image in cluster"]
    )
    cmd = [arg.command]

    if arg.kubeconfig != "":
        if arg.command == "oc":
            cmd += ["--config", arg.kubeconfig]
        else:
            cmd += ["--kubeconfig", arg.kubeconfig]
    if arg.pvcname != "":
        cmd += ['get', 'pvc', arg.pvcname, '-o', 'json']
        # list all pvc and get mapping
    else:
        cmd += ['get', 'pvc', '-o', 'json']
    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to list pvc %s", stderr)
        sys.exit()
    try:
        pvcs = json.loads(stdout)
    except ValueError as err:
        print(err, stdout)
        sys.exit()
    if arg.pvcname != "":
        format_table(arg, pvcs, table)
    else:
        for pvc in pvcs['items']:
            format_table(arg, pvc, table)
    print(table)

def format_table(arg, pvc_data, table):
    """
    format table for pvc and image information
    """
    # pvc name
    pvcname = pvc_data['metadata']['name']
    # get pv name
    pvname = pvc_data['spec']['volumeName']
    # get volume handler from pv
    volume_name = get_volume_handler_from_pv(arg, pvname)
    # get volume handler
    if volume_name == "":
        table.add_row([pvcname, "", "", False,
                       False, False])
        return
    pool_name = get_pool_name(arg, volume_name)
    if pool_name == "":
        table.add_row([pvcname, pvname, "", False,
                       False, False])
        return
    # get image id
    image_id = get_image_uuid(volume_name)
    if image_id is None:
        table.add_row([pvcname, pvname, "", False,
                       False, False])
        return
    # check image details present rados omap
    pv_present, uuid_present = validate_volume_in_rados(
        arg, image_id, pvname, pool_name)
    image_in_cluster = check_image_in_cluster(arg, image_id, pool_name)
    image_name = "csi-vol-%s" % image_id
    table.add_row([pvcname, pvname, image_name, pv_present,
                   uuid_present, image_in_cluster])


def validate_volume_in_rados(arg, image_id, pvc_name, pool_name):
    """
    validate volume information in rados
    """

    pv_present = check_pv_name_in_rados(arg, image_id, pvc_name, pool_name)
    uuid_present = check_image_uuid_in_rados(
        arg, image_id, pvc_name, pool_name)
    return pv_present, uuid_present


def check_pv_name_in_rados(arg, image_id, pvc_name, pool_name):
    """
    validate pvc information in rados
    """
    omapkey = 'csi.volume.%s' % pvc_name
    cmd = ['rados', 'getomapval', 'csi.volumes.default',
           omapkey, "--pool", pool_name]
    if not arg.userkey:
        cmd += ["--id", arg.userid, "--key", arg.userkey]
    if arg.toolboxdeployed is True:
        tool_box_name = get_tool_box_pod_name(arg)
        kube = [arg.command]
        if arg.kubeconfig != "":
            if arg.command == "oc":
                kube += ["--config", arg.kubeconfig]
            else:
                kube += ["--kubeconfig", arg.kubeconfig]
        kube += ['exec', '-it', tool_box_name, '-n',
                 arg.rooknamespace, '--']
        cmd = kube+cmd
    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

    stdout, stderr = out.communicate()
    if stderr is not None:
        return False
    name = ''
    lines = [x.strip() for x in stdout.split("\n")]
    for line in lines:
        if ' ' not in line:
            continue
        if 'value' in line and 'bytes' in line:
            continue
        part = re.findall(r'[A-Za-z0-9\-]+', line)
        if part:
            name += part[-1]
    if name != image_id:
        if arg.debug:
            print("expected image Id %s found Id in rados %s" %
                  (image_id, name))
        return False
    return True


def check_image_in_cluster(arg, image_uuid, pool_name):
    """
    validate pvc information in ceph backend
    """
    image = "csi-vol-%s" % image_uuid
    cmd = ['rbd', 'info', image, "--pool", pool_name]
    if not arg.userkey:
        cmd += ["--id", arg.userid, "--key", arg.userkey]
    if arg.toolboxdeployed is True:
        tool_box_name = get_tool_box_pod_name(arg)
        kube = [arg.command]
        if arg.kubeconfig != "":
            if arg.command == "oc":
                kube += ["--config", arg.kubeconfig]
            else:
                kube += ["--kubeconfig", arg.kubeconfig]
        kube += ['exec', '-it', tool_box_name, '-n',
                 arg.rooknamespace, '--']
        cmd = kube+cmd

    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to toolbox %s", stderr)
        return False
    if "No such file or directory" in stdout:
        if arg.debug:
            print("image not found in cluster", stdout)
        return False
    return True

#pylint: disable=too-many-branches
def check_image_uuid_in_rados(arg, image_id, pvc_name, pool_name):
    """
    validate image uuid in rados
    """
    omapkey = 'csi.volume.%s' % image_id
    cmd = ['rados', 'getomapval', omapkey, "csi.volname", "--pool", pool_name]
    if not arg.userkey:
        cmd += ["--id", arg.userid, "--key", arg.userkey]
    if arg.toolboxdeployed is True:
        kube = [arg.command]
        if arg.kubeconfig != "":
            if arg.command == "oc":
                kube += ["--config", arg.kubeconfig]
            else:
                kube += ["--kubeconfig", arg.kubeconfig]
        tool_box_name = get_tool_box_pod_name(arg)
        kube += ['exec', '-it', tool_box_name, '-n',
                 arg.rooknamespace, '--']
        cmd = kube+cmd

    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to toolbox %s", stderr)
        return False

    name = ''
    lines = [x.strip() for x in stdout.split("\n")]
    for line in lines:
        if ' ' not in line:
            continue
        if 'value' in line and 'bytes' in line:
            continue
        part = re.findall(r'[A-Za-z0-9\-]+', line)
        if part:
            name += part[-1]
    if name != pvc_name:
        if arg.debug:
            print("expected image Id %s found Id in rados %s" %
                  (pvc_name, name))
        return False
    return True


def get_image_uuid(volume_handler):
    """
    fetch image uuid from volume handler
    """
    image_id = volume_handler.split('-')
    if len(image_id) < 9:
        return None
    img_id = "-"
    return img_id.join(image_id[len(image_id)-5:])


def get_volume_handler_from_pv(arg, pvname):
    """
    fetch volume handler from pv
    """
    cmd = [arg.command]
    if arg.kubeconfig != "":
        if arg.command == "oc":
            cmd += ["--config", arg.kubeconfig]
        else:
            cmd += ["--kubeconfig", arg.kubeconfig]

    cmd += ['get', 'pv', pvname, '-o', 'json']
    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to pv %s", stderr)
        return ""
    try:
        vol = json.loads(stdout)
        return vol['spec']['csi']['volumeHandle']
    except ValueError as err:
        if arg.debug:
            print("failed to pv %s", err)
    return ""

def get_tool_box_pod_name(arg):
    """
    get tool box pod name
    """
    cmd = [arg.command]
    if arg.kubeconfig != "":
        if arg.command == "oc":
            cmd += ["--config", arg.kubeconfig]
        else:
            cmd += ["--kubeconfig", arg.kubeconfig]
    cmd += ['get', 'po', '-l=app=rook-ceph-tools',
            '-n', arg.rooknamespace, '-o', 'json']
    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to get toolbox pod name %s", stderr)
        return ""
    try:
        pod_name = json.loads(stdout)
        return pod_name['items'][0]['metadata']['name']
    except ValueError as err:
        if arg.debug:
            print("failed to pod %s", err)
    return ""

#pylint: disable=too-many-branches
def get_pool_name(arg, vol_id):
    """
    get pool name from ceph backend
    """
    cmd = ['ceph', 'osd', 'lspools', '--format=json']
    if  not arg.userkey:
        cmd += ["--id", arg.userid, "--key", arg.userkey]
    if arg.toolboxdeployed is True:
        kube = [arg.command]
        if arg.kubeconfig != "":
            if arg.command == "oc":
                kube += ["--config", arg.kubeconfig]
            else:
                kube += ["--kubeconfig", arg.kubeconfig]
        tool_box_name = get_tool_box_pod_name(arg)
        kube += ['exec', '-it', tool_box_name, '-n',
                 arg.rooknamespace, '--']
        cmd = kube+cmd
    out = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

    stdout, stderr = out.communicate()
    if stderr is not None:
        if arg.debug:
            print("failed to pool name %s", stderr)
        return ""
    try:
        pools = json.loads(stdout)
    except ValueError as err:
        if arg.debug:
            print("failed to pool name %s", err)
        return ""
    pool_id = vol_id.split('-')
    if len(pool_id) < 4:
        if arg.debug:
            print("pood id notin proper format", pool_id)
        return ""
    if pool_id[3] in arg.rooknamespace:
        pool_id = pool_id[4]
    else:
        pool_id = pool_id[3]
    for pool in pools:
        if int(pool_id) is int(pool['poolnum']):
            return pool['poolname']
    return ""


if __name__ == "__main__":
    ARGS = PARSER.parse_args()
    if ARGS.command not in ["kubectl", "oc"]:
        print("%s command not supported" % ARGS.command)
        sys.exit(1)
    list_pvc_vol_name_mapping(ARGS)
