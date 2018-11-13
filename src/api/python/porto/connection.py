import os
import socket
import threading

from . import rpc_pb2
from . import exceptions
from .container import Container
from .volume import Layer, Storage, MetaStorage, VolumeLink, Volume

def _encode_message(msg, val, key=None):
    msg.SetInParent()
    if isinstance(val, dict):
        if key is not None:
            msg = getattr(msg, key)
            msg.SetInParent()
        for k, v in val.items():
            _encode_message(msg, v, k)
    elif isinstance(val, list):
        if key is not None:
            msg = getattr(msg, key)
        if isinstance(val[0], dict):
            for item in val:
                _encode_message(msg.add(), item)
        else:
            msg.extend(val)
    else:
        setattr(msg, key, val)


def _decode_message(msg):
    ret = dict()
    for dsc, val in msg.ListFields():
        key = dsc.name
        if dsc.type == dsc.TYPE_MESSAGE:
            if dsc.label == dsc.LABEL_REPEATED:
                ret[key] = [_decode_message(v) for v in val]
            else:
                ret[key] = _decode_message(val)
        elif dsc.label == dsc.LABEL_REPEATED:
            ret[key] = list(val)
        else:
            ret[key] = val
    return ret


class _RPC(object):
    def __init__(self, socket_path, timeout, socket_constructor,
                 lock_constructor, auto_reconnect):
        self.lock = lock_constructor()
        self.socket_path = socket_path
        self.timeout = timeout
        self.socket_constructor = socket_constructor
        self.sock = None
        self.sock_pid = None
        self.auto_reconnect = auto_reconnect
        self.async_wait_names = []
        self.async_wait_callback = None
        self.async_wait_timeout = None

    def _set_timeout(self, extra_timeout=0):
        if extra_timeout is None:
            self.sock.settimeout(None)
        else:
            self.sock.settimeout(self.timeout + extra_timeout)

    def set_timeout(self, timeout):
        with self.lock:
            self.timeout = timeout
            if self.sock is not None:
                self._set_timeout()

    def _connect(self):
        try:
            SOCK_CLOEXEC = 0o2000000
            self.sock = self.socket_constructor(socket.AF_UNIX, socket.SOCK_STREAM | SOCK_CLOEXEC)
            self._set_timeout()
            self.sock.connect(self.socket_path)
        except socket.timeout as e:
            self.sock = None
            raise exceptions.SocketTimeout("Porto connection timeout: {}".format(e))
        except socket.error as e:
            self.sock = None
            raise exceptions.SocketError("Porto connection error: {}".format(e))

        self.sock_pid = os.getpid()
        self._resend_async_wait()

    def _recv_data(self, count):
        msg = bytearray()
        while len(msg) < count:
            chunk = self.sock.recv(count - len(msg))
            if not chunk:
                raise socket.error(socket.errno.ECONNRESET, os.strerror(socket.errno.ECONNRESET))
            msg.extend(chunk)
        return msg

    def _recv_response(self):
        rsp = rpc_pb2.TPortoResponse()
        while True:
            length = shift = 0
            while True:
                b = self._recv_data(1)
                length |= (b[0] & 0x7f) << shift
                shift += 7
                if b[0] <= 0x7f:
                    break

            rsp.ParseFromString(bytes(self._recv_data(length)))

            if rsp.HasField('AsyncWait'):
                if self.async_wait_callback is not None:
                    if rsp.AsyncWait.HasField("label"):
                        self.async_wait_callback(name=rsp.AsyncWait.name, state=rsp.AsyncWait.state, when=rsp.AsyncWait.when, label=rsp.AsyncWait.label, value=rsp.AsyncWait.value)
                    else:
                        self.async_wait_callback(name=rsp.AsyncWait.name, state=rsp.AsyncWait.state, when=rsp.AsyncWait.when)
            else:
                return rsp

    def encode_request(self, request):
        req = request.SerializeToString()
        length = len(req)
        hdr = bytearray()
        while length > 0x7f:
            hdr.append(0x80 | (length & 0x7f))
            length >>= 7
        hdr.append(length)
        return hdr + req

    def call(self, request, extra_timeout=0):
        req = self.encode_request(request)

        with self.lock:
            if self.sock is None:
                if self.auto_reconnect:
                    self._connect()
                else:
                    raise exceptions.SocketError("Porto socket is not connected")
            elif self.sock_pid != os.getpid():
                if self.auto_reconnect:
                    self._connect()
                else:
                    raise exceptions.SocketError("Porto socket connected by other pid {}".format(self.sock_pid))
            elif self.auto_reconnect:
                try:
                    self.sock.sendall(req)
                    req = None
                except socket.timeout as e:
                    self.sock = None
                    raise exceptions.SocketTimeout("Porto connection timeout: {}".format(e))
                except socket.error as e:
                    self._connect()

            try:
                if req is not None:
                    self.sock.sendall(req)

                if extra_timeout is None or extra_timeout > 0:
                    self._set_timeout(extra_timeout)

                response = self._recv_response()

                if extra_timeout is None or extra_timeout > 0:
                    self._set_timeout()

            except socket.timeout as e:
                self.sock = None
                raise exceptions.SocketTimeout("Porto connection timeout: {}".format(e))
            except socket.error as e:
                self.sock = None
                raise exceptions.SocketError("Socket error: {}".format(e))

        if response.error != rpc_pb2.Success:
            raise exceptions.PortoException.Create(response.error, response.errorMsg)

        return response

    def connect(self):
        with self.lock:
            self._connect()

    def disconnect(self):
        with self.lock:
            if self.sock is not None:
                self.sock.close()
                self.sock = None

    def connected(self):
        with self.lock:
            return self.sock is not None

    def _resend_async_wait(self):
        if not self.async_wait_names:
            return

        request = rpc_pb2.TPortoRequest()
        request.AsyncWait.name.extend(self.async_wait_names)
        if self.async_wait_timeout is not None:
            request.AsyncWait.timeout_ms = int(self.async_wait_timeout * 1000)

        self.sock.sendall(self.encode_request(request))
        response = self._recv_response()
        if response.error != rpc_pb2.Success:
            raise exceptions.PortoException.Create(response.error, response.errorMsg)

    def async_wait(self, names, labels, callback, timeout):
        with self.lock:
            self.async_wait_names = names
            self.async_wait_callback = callback
            self.async_wait_timeout = timeout

        request = rpc_pb2.TPortoRequest()
        request.AsyncWait.name.extend(names)
        if timeout is not None:
            request.AsyncWait.timeout_ms = int(timeout * 1000)
        if labels is not None:
            request.AsyncWait.label.extend(labels)

        self.call(request)


class Property(object):
    def __init__(self, name, desc, read_only, dynamic):
        self.name = name
        self.desc = desc
        self.read_only = read_only
        self.dynamic = dynamic

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'Property `{}` `{}`'.format(self.name, self.desc)


class Connection(object):
    def __init__(self,
                 socket_path='/run/portod.socket',
                 timeout=300,
                 disk_timeout=900,
                 socket_constructor=socket.socket,
                 lock_constructor=threading.Lock,
                 auto_reconnect=True):
        self.rpc = _RPC(socket_path=socket_path,
                        timeout=timeout,
                        socket_constructor=socket_constructor,
                        lock_constructor=lock_constructor,
                        auto_reconnect=auto_reconnect)
        self.disk_timeout = disk_timeout

    def Connect(self):
        self.rpc.connect()

    def Disconnect(self):
        self.rpc.disconnect()

    def connect(self):
        self.Connect()

    def disconnect(self):
        self.Disconnect()

    def Connected(self):
        return self.rpc.connected()

    def GetTimeout(self):
        return self.rpc.timeout

    def SetTimeout(self, timeout):
        self.rpc.set_timeout(timeout)

    def GetDiskTimeout(self):
        return self.disk_timeout

    def SetDiskTimeout(self, disk_timeout):
        self.disk_timeout = disk_timeout

    def SetAutoReconnect(self, auto_reconnect):
        self.rpc.auto_reconnect = auto_reconnect

    def Call(self, command_name, response_name=None, extra_timeout=0, **kwargs):
        req = rpc_pb2.TPortoRequest()
        cmd = getattr(req, command_name)
        cmd.SetInParent()
        _encode_message(cmd, kwargs)
        rsp = self.rpc.call(req, extra_timeout)
        if hasattr(rsp, response_name or command_name):
            return _decode_message(getattr(rsp, response_name or command_name))
        return None

    def List(self, mask=None):
        request = rpc_pb2.TPortoRequest()
        request.List.SetInParent()
        if mask is not None:
            request.List.mask = mask
        return self.rpc.call(request).List.name

    def ListContainers(self, mask=None):
        return [Container(self, name) for name in self.List(mask)]

    def FindLabel(self, label, mask=None, state=None, value=None):
        request = rpc_pb2.TPortoRequest()
        request.FindLabel.label = label
        if mask is not None:
            request.FindLabel.mask = mask
        if state is not None:
            request.FindLabel.state = state
        if value is not None:
            request.FindLabel.value = value
        list = self.rpc.call(request).FindLabel.list
        return [{'name': l.name, 'state': l.state, 'label': l.label, 'value': l.value} for l in list]

    def Find(self, name):
        self.GetProperty(name, "state")
        return Container(self, name)

    def Create(self, name, weak=False):
        request = rpc_pb2.TPortoRequest()
        if weak:
            request.CreateWeak.name = name
        else:
            request.Create.name = name
        self.rpc.call(request)
        return Container(self, name)

    def CreateWeakContainer(self, name):
        return self.Create(name, weak=True)

    def Run(self, name, weak=True, start=True, wait=0, root_volume=None, private_value=None, **kwargs):
        ct = self.Create(name, weak=True)
        try:
            for prop, value in kwargs.items():
                ct.SetProperty(prop, value)
            if private_value is not None:
                ct.SetProperty('private', private_value)
            if root_volume is not None:
                root = self.CreateVolume(containers=name, **root_volume)
                ct.SetProperty('root', root.path)
            if start:
                ct.Start()
            if not weak:
                ct.SetProperty('weak', False)
            if wait != 0:
                ct.WaitContainer(wait)
        except exceptions.PortoException as e:
            try:
                ct.Destroy()
            except exceptions.ContainerDoesNotExist:
                pass
            raise e
        return ct

    def Destroy(self, container):
        if isinstance(container, Container):
            container = container.name
        request = rpc_pb2.TPortoRequest()
        request.Destroy.name = container
        self.rpc.call(request)

    def Start(self, name, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.Start.name = name
        self.rpc.call(request, timeout)

    def Stop(self, name, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.Stop.name = name
        if timeout is not None and timeout >= 0:
            request.Stop.timeout_ms = timeout * 1000
        else:
            timeout = 30
        self.rpc.call(request, timeout)

    def Kill(self, name, sig):
        request = rpc_pb2.TPortoRequest()
        request.Kill.name = name
        request.Kill.sig = sig
        self.rpc.call(request)

    def Pause(self, name):
        request = rpc_pb2.TPortoRequest()
        request.Pause.name = name
        self.rpc.call(request)

    def Resume(self, name):
        request = rpc_pb2.TPortoRequest()
        request.Resume.name = name
        self.rpc.call(request)

    def Get(self, containers, variables, nonblock=False, sync=False):
        request = rpc_pb2.TPortoRequest()
        request.Get.name.extend(containers)
        request.Get.variable.extend(variables)
        request.Get.sync = sync
        if nonblock:
            request.Get.nonblock = nonblock
        resp = self.rpc.call(request)
        res = {}
        for container in resp.Get.list:
            var = {}
            for kv in container.keyval:
                if kv.HasField('error'):
                    var[kv.variable] = exceptions.PortoException.Create(kv.error, kv.errorMsg)
                    continue
                if kv.value == 'false':
                    var[kv.variable] = False
                elif kv.value == 'true':
                    var[kv.variable] = True
                else:
                    var[kv.variable] = kv.value

            res[container.name] = var
        return res

    def GetProperty(self, name, prop, index=None, sync=False, real=False):
        request = rpc_pb2.TPortoRequest()
        request.GetProperty.name = name
        if type(prop) is tuple:
            request.GetProperty.property = prop[0] + "[" + prop[1] + "]"
        elif index is not None:
            request.GetProperty.property = prop + "[" + index + "]"
        else:
            request.GetProperty.property = prop
        request.GetProperty.sync = sync
        request.GetProperty.real = real
        res = self.rpc.call(request).GetProperty.value
        if res == 'false':
            return False
        elif res == 'true':
            return True
        return res

    def SetProperty(self, name, prop, value, index=None):
        if value is False:
            value = 'false'
        elif value is True:
            value = 'true'
        elif value is None:
            value = ''
        else:
            value = str(value)

        request = rpc_pb2.TPortoRequest()
        request.SetProperty.name = name

        if type(prop) is tuple:
            request.SetProperty.property = prop[0] + "[" + prop[1] + "]"
        elif index is not None:
            request.SetProperty.property = prop + "[" + index + "]"
        else:
            request.SetProperty.property = prop

        request.SetProperty.value = value
        self.rpc.call(request)

    def Set(self, container, **kwargs):
        for prop, value in kwargs.items():
            self.SetProperty(container, prop, value)

    def GetData(self, name, data, sync=False):
        request = rpc_pb2.TPortoRequest()
        request.GetDataProperty.name = name
        request.GetDataProperty.data = data
        request.GetDataProperty.sync = sync
        res = self.rpc.call(request).GetDataProperty.value
        if res == 'false':
            return False
        elif res == 'true':
            return True
        return res

    def GetInt(self, name, prop, index=None):
        request = rpc_pb2.TPortoRequest()
        request.GetIntProperty.name = name
        if type(prop) is tuple:
            request.GetIntProperty.property = prop[0]
            request.GetIntProperty.index = prop[1]
        else:
            request.GetIntProperty.property = prop
            if index is not None:
                request.GetIntProperty.index = index
        try:
            return self.rpc.call(request).GetIntProperty.value
        except exceptions.InvalidMethod:
            val = self.GetProperty(name, prop, index=index, real=True)
            try:
                return int(val)
            except ValueError:
                raise exceptions.InvalidValue("Non integer value: {}".format(val))
        except:
            raise

    def SetInt(self, name, prop, value, index=None):
        request = rpc_pb2.TPortoRequest()
        request.SetIntProperty.name = name
        if type(prop) is tuple:
            request.SetIntProperty.property = prop[0]
            request.SetIntProperty.index = prop[1]
        else:
            request.SetIntProperty.property = prop
            if index is not None:
                request.SetIntProperty.index = index
        request.SetIntProperty.value = value
        try:
            self.rpc.call(request)
        except exceptions.InvalidMethod:
            self.SetProperty(name, prop, value, index=index)
        except:
            raise

    def ContainerProperties(self):
        request = rpc_pb2.TPortoRequest()
        request.ListProperties.SetInParent()
        res = {}
        for prop in self.rpc.call(request).ListProperties.list:
            res[prop.name] = Property(prop.name, prop.desc, prop.read_only, prop.dynamic)
        return res

    def VolumeProperties(self):
        request = rpc_pb2.TPortoRequest()
        request.ListVolumeProperties.SetInParent()
        res = {}
        for prop in self.rpc.call(request).ListVolumeProperties.list:
            res[prop.name] = Property(prop.name, prop.desc, False, False)
        return res

    def Plist(self):
        request = rpc_pb2.TPortoRequest()
        request.ListProperties.SetInParent()
        return [item.name for item in self.rpc.call(request).ListProperties.list]

    # deprecated - now they properties
    def Dlist(self):
        request = rpc_pb2.TPortoRequest()
        request.ListDataProperties.SetInParent()
        return [item.name for item in self.rpc.call(request).ListDataProperties.list]

    def Vlist(self):
        request = rpc_pb2.TPortoRequest()
        request.ListVolumeProperties.SetInParent()
        result = self.rpc.call(request).ListVolumeProperties.list
        return [prop.name for prop in result]

    def WaitContainers(self, containers, timeout=None, labels=None):
        request = rpc_pb2.TPortoRequest()
        for ct in containers:
            request.Wait.name.append(str(ct))
        if timeout is not None and timeout >= 0:
            request.Wait.timeout_ms = int(timeout * 1000)
        else:
            timeout = None
        if labels is not None:
            request.Wait.label.extend(labels)
        resp = self.rpc.call(request, timeout)
        if resp.Wait.name == "":
            raise exceptions.WaitContainerTimeout("Timeout {} exceeded".format(timeout))
        return resp.Wait.name

    # legacy compat - timeout in ms
    def Wait(self, containers, timeout=None, timeout_s=None, labels=None):
        if timeout_s is not None:
            timeout = timeout_s
        elif timeout is not None and timeout >= 0:
            timeout = timeout / 1000.
        try:
            return self.WaitContainers(containers, timeout, labels=labels)
        except exceptions.WaitContainerTimeout:
            return ""

    def AsyncWait(self, containers, callback, timeout=None, labels=None):
        self.rpc.async_wait([str(ct) for ct in containers], labels, callback, timeout)

    def WaitLabels(self, containers, labels, timeout=None):
        request = rpc_pb2.TPortoRequest()
        for ct in containers:
            request.Wait.name.append(str(ct))
        if timeout is not None and timeout >= 0:
            request.Wait.timeout_ms = int(timeout * 1000)
        else:
            timeout = None
        request.Wait.label.extend(labels)
        resp = self.rpc.call(request, timeout)
        if resp.Wait.name == "":
            raise exceptions.WaitContainerTimeout("Timeout {} exceeded".format(timeout))
        return _decode_message(resp.Wait)

    def GetLabel(self, container, label):
        return self.GetProperty(container, 'labels', label)

    def SetLabel(self, container, label, value, prev_value=None, state=None):
        req = rpc_pb2.TPortoRequest()
        req.SetLabel.name = str(container)
        req.SetLabel.label = label
        req.SetLabel.value = value
        if prev_value is not None:
            req.SetLabel.prev_value = prev_value
        if state is not None:
            req.SetLabel.state = state
        self.rpc.call(req)

    def IncLabel(self, container, label, add=1):
        req = rpc_pb2.TPortoRequest()
        req.IncLabel.name = str(container)
        req.IncLabel.label = label
        req.IncLabel.add = add
        return self.rpc.call(req).IncLabel.result

    def CreateVolume(self, path=None, layers=None, storage=None, private_value=None, timeout=None, **properties):
        if layers:
            layers = [l.name if isinstance(l, Layer) else l for l in layers]
            properties['layers'] = ';'.join(layers)

        if storage is not None:
            properties['storage'] = str(storage)

        if private_value is not None:
            properties['private'] = private_value

        request = rpc_pb2.TPortoRequest()
        request.CreateVolume.SetInParent()
        if path:
            request.CreateVolume.path = path
        for name, value in properties.items():
            prop = request.CreateVolume.properties.add()
            prop.name, prop.value = name, value
        pb = self.rpc.call(request, timeout or self.disk_timeout).CreateVolume
        return Volume(self, pb.path, pb)

    def FindVolume(self, path):
        pb = self._ListVolumes(path=path)[0]
        return Volume(self, path, pb)

    def NewVolume(self, spec, timeout=None):
        req = rpc_pb2.TPortoRequest()
        req.NewVolume.SetInParent()
        _encode_message(req.NewVolume.volume, spec)
        rsp = self.rpc.call(req, timeout or self.disk_timeout)
        return _decode_message(rsp.NewVolume.volume)

    def GetVolume(self, path, container=None, timeout=None):
        req = rpc_pb2.TPortoRequest()
        req.GetVolume.SetInParent()
        if container is not None:
            req.GetVolume.container = str(container)
        req.GetVolume.path.append(path)
        rsp = self.rpc.call(req, timeout or self.disk_timeout)
        return _decode_message(rsp.GetVolume.volume[0])

    def GetVolumes(self, paths=None, container=None, labels=None, timeout=None):
        req = rpc_pb2.TPortoRequest()
        req.GetVolume.SetInParent()
        if container is not None:
            req.GetVolume.container = str(container)
        if paths is not None:
            req.GetVolume.path.extend(paths)
        if labels is not None:
            req.GetVolume.label.extend(labels)
        rsp = self.rpc.call(req, timeout or self.disk_timeout)
        return [_decode_message(v) for v in rsp.GetVolume.volume]

    def LinkVolume(self, path, container, target=None, read_only=False, required=False):
        request = rpc_pb2.TPortoRequest()
        if target is not None or required:
            command = request.LinkVolumeTarget
        else:
            command = request.LinkVolume
        command.path = path
        command.container = container
        if target is not None:
            command.target = target
        if read_only:
            command.read_only = True
        if required:
            command.required = True
        self.rpc.call(request)

    def UnlinkVolume(self, path, container=None, target=None, strict=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        if target is not None:
            command = request.UnlinkVolumeTarget
        else:
            command = request.UnlinkVolume
        command.path = path
        if container:
            command.container = container
        if target is not None:
            command.target = target
        if strict is not None:
            command.strict = strict
        self.rpc.call(request, timeout or self.disk_timeout)

    def DestroyVolume(self, volume, strict=None, timeout=None):
        self.UnlinkVolume(volume.path if isinstance(volume, Volume) else volume, '***', strict=strict, timeout=timeout)

    def _ListVolumes(self, path=None, container=None):
        if isinstance(container, Container):
            container = container.name
        request = rpc_pb2.TPortoRequest()
        request.ListVolumes.SetInParent()
        if path:
            request.ListVolumes.path = path
        if container:
            request.ListVolumes.container = container
        return self.rpc.call(request).ListVolumes.volumes

    def ListVolumes(self, container=None):
        return [Volume(self, v.path, v) for v in self._ListVolumes(container)]

    def ListVolumeLinks(self, volume=None, container=None):
        links = []
        for v in self._ListVolumes(path=volume.path if isinstance(volume, Volume) else volume, container=container):
            for l in v.links:
                links.append(VolumeLink(Volume(self, v.path, v), Container(self, l.container), l.target, l.read_only, l.required))
        return links

    def GetVolumeProperties(self, path):
        return {p.name: p.value for p in self._ListVolumes(path=path)[0].properties}

    def TuneVolume(self, path, **properties):
        request = rpc_pb2.TPortoRequest()
        request.TuneVolume.SetInParent()
        request.TuneVolume.path = path
        for name, value in properties.items():
            prop = request.TuneVolume.properties.add()
            prop.name, prop.value = name, value
        self.rpc.call(request)

    def SetVolumeLabel(self, path, label, value, prev_value=None):
        req = rpc_pb2.TPortoRequest()
        req.SetVolumeLabel.path = path
        req.SetVolumeLabel.label = label
        req.SetVolumeLabel.value = value
        if prev_value is not None:
            req.SetVolumeLabel.prev_value = prev_value
        self.rpc.call(req).SetVolumeLabel.prev_value

    def ImportLayer(self, layer, tarball, place=None, private_value=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ImportLayer.layer = layer
        request.ImportLayer.tarball = tarball
        request.ImportLayer.merge = False
        if place is not None:
            request.ImportLayer.place = place
        if private_value is not None:
            request.ImportLayer.private_value = private_value

        self.rpc.call(request, timeout or self.disk_timeout)
        return Layer(self, layer, place)

    def MergeLayer(self, layer, tarball, place=None, private_value=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ImportLayer.layer = layer
        request.ImportLayer.tarball = tarball
        request.ImportLayer.merge = True
        if place is not None:
            request.ImportLayer.place = place
        if private_value is not None:
            request.ImportLayer.private_value = private_value
        self.rpc.call(request, timeout or self.disk_timeout)
        return Layer(self, layer, place)

    def RemoveLayer(self, layer, place=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.RemoveLayer.layer = layer
        if place is not None:
            request.RemoveLayer.place = place
        self.rpc.call(request, timeout or self.disk_timeout)

    def GetLayerPrivate(self, layer, place=None):
        request = rpc_pb2.TPortoRequest()
        request.GetLayerPrivate.layer = layer
        if place is not None:
            request.GetLayerPrivate.place = place
        return self.rpc.call(request).GetLayerPrivate.private_value

    def SetLayerPrivate(self, layer, private_value, place=None):
        request = rpc_pb2.TPortoRequest()
        request.SetLayerPrivate.layer = layer
        request.SetLayerPrivate.private_value = private_value

        if place is not None:
            request.SetLayerPrivate.place = place
        self.rpc.call(request)

    def ExportLayer(self, volume, tarball, place=None, compress=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ExportLayer.volume = volume
        request.ExportLayer.tarball = tarball
        if place is not None:
            request.ExportLayer.place = place
        if compress is not None:
            request.ExportLayer.compress = compress
        self.rpc.call(request, timeout or self.disk_timeout)

    def ReExportLayer(self, layer, tarball, place=None, compress=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ExportLayer.volume = ""
        request.ExportLayer.layer = layer
        request.ExportLayer.tarball = tarball
        if place is not None:
            request.ExportLayer.place = place
        if compress is not None:
            request.ExportLayer.compress = compress
        self.rpc.call(request, timeout or self.disk_timeout)

    def _ListLayers(self, place=None, mask=None):
        request = rpc_pb2.TPortoRequest()
        request.ListLayers.SetInParent()
        if place is not None:
            request.ListLayers.place = place
        if mask is not None:
            request.ListLayers.mask = mask
        return self.rpc.call(request).ListLayers

    def ListLayers(self, place=None, mask=None):
        response = self._ListLayers(place, mask)
        if response.layers:
            return [Layer(self, l.name, place, l) for l in response.layers]
        return [Layer(self, l, place) for l in response.layer]

    def FindLayer(self, layer, place=None):
        response = self._ListLayers(place, layer)
        if layer not in response.layer:
            raise exceptions.LayerNotFound("layer `%s` not found" % layer)
        if response.layers and response.layers[0].name == layer:
            return Layer(self, layer, place, response.layers[0])
        return Layer(self, layer, place)

    def _ListStorages(self, place=None, mask=None):
        request = rpc_pb2.TPortoRequest()
        request.ListStorages.SetInParent()
        if place is not None:
            request.ListStorages.place = place
        if mask is not None:
            request.ListStorages.mask = mask
        return self.rpc.call(request).ListStorages

    def ListStorages(self, place=None, mask=None):
        return [Storage(self, s.name, place, s) for s in self._ListStorages(place, mask).storages]

    # deprecated
    def ListStorage(self, place=None, mask=None):
        return [Storage(self, s.name, place, s) for s in self._ListStorages(place, mask).storages]

    def FindStorage(self, name, place=None):
        response = self._ListStorages(place, name)
        if not response.storages:
            raise exceptions.VolumeNotFound("storage `%s` not found" % name)
        return Storage(self, name, place, response.storages[0])

    def ListMetaStorages(self, place=None, mask=None):
        return [MetaStorage(self, s.name, place, s) for s in self._ListStorages(place, mask).meta_storages]

    def FindMetaStorage(self, name, place=None):
        response = self._ListStorages(place, name + "/")
        if not response.meta_storages:
            raise exceptions.VolumeNotFound("meta storage `%s` not found" % name)
        return MetaStorage(self, name, place, response.meta_storages[0])

    def RemoveStorage(self, name, place=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.RemoveStorage.name = name
        if place is not None:
            request.RemoveStorage.place = place
        self.rpc.call(request, timeout or self.disk_timeout)

    def ImportStorage(self, name, tarball, place=None, private_value=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ImportStorage.name = name
        request.ImportStorage.tarball = tarball
        if place is not None:
            request.ImportStorage.place = place
        if private_value is not None:
            request.ImportStorage.private_value = private_value
        self.rpc.call(request, timeout or self.disk_timeout)
        return Storage(self, name, place)

    def ExportStorage(self, name, tarball, place=None, timeout=None):
        request = rpc_pb2.TPortoRequest()
        request.ExportStorage.name = name
        request.ExportStorage.tarball = tarball
        if place is not None:
            request.ExportStorage.place = place
        self.rpc.call(request, timeout or self.disk_timeout)

    def CreateMetaStorage(self, name, place=None, private_value=None, space_limit=None, inode_limit=None):
        request = rpc_pb2.TPortoRequest()
        request.CreateMetaStorage.name = name
        if place is not None:
            request.CreateMetaStorage.place = place
        if private_value is not None:
            request.CreateMetaStorage.private_value = private_value
        if space_limit is not None:
            request.CreateMetaStorage.space_limit = space_limit
        if inode_limit is not None:
            request.CreateMetaStorage.inode_limit = inode_limit
        self.rpc.call(request)
        return MetaStorage(self, name, place)

    def ResizeMetaStorage(self, name, place=None, private_value=None, space_limit=None, inode_limit=None):
        request = rpc_pb2.TPortoRequest()
        request.ResizeMetaStorage.name = name
        if place is not None:
            request.ResizeMetaStorage.place = place
        if private_value is not None:
            request.ResizeMetaStorage.private_value = private_value
        if space_limit is not None:
            request.ResizeMetaStorage.space_limit = space_limit
        if inode_limit is not None:
            request.ResizeMetaStorage.inode_limit = inode_limit
        self.rpc.call(request)

    def RemoveMetaStorage(self, name, place=None):
        request = rpc_pb2.TPortoRequest()
        request.RemoveMetaStorage.name = name
        if place is not None:
            request.RemoveMetaStorage.place = place
        self.rpc.call(request)

    def ConvertPath(self, path, source, destination):
        request = rpc_pb2.TPortoRequest()
        request.ConvertPath.path = path
        request.ConvertPath.source = source
        request.ConvertPath.destination = destination
        return self.rpc.call(request).convertPath.path

    def SetSymlink(self, name, symlink, target):
        request = rpc_pb2.TPortoRequest()
        request.SetSymlink.container = name
        request.SetSymlink.symlink = symlink
        request.SetSymlink.target = target
        self.rpc.call(request)

    def AttachProcess(self, name, pid, comm=""):
        request = rpc_pb2.TPortoRequest()
        request.AttachProcess.name = name
        request.AttachProcess.pid = pid
        request.AttachProcess.comm = comm
        self.rpc.call(request)

    def AttachThread(self, name, pid, comm=""):
        request = rpc_pb2.TPortoRequest()
        request.AttachThread.name = name
        request.AttachThread.pid = pid
        request.AttachThread.comm = comm
        self.rpc.call(request)

    def LocateProcess(self, pid, comm=""):
        request = rpc_pb2.TPortoRequest()
        request.LocateProcess.pid = pid
        request.LocateProcess.comm = comm
        name = self.rpc.call(request).LocateProcess.name
        return Container(self, name)

    def Version(self):
        request = rpc_pb2.TPortoRequest()
        request.Version.SetInParent()
        response = self.rpc.call(request)
        return (response.Version.tag, response.Version.revision)
