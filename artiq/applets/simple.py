import logging
import argparse
import asyncio
import os
import string

from qasync import QEventLoop, QtWidgets, QtCore

from sipyco.sync_struct import Subscriber, process_mod
from sipyco.pc_rpc import AsyncioClient as RPCClient
from sipyco import pyon
from sipyco.pipe_ipc import AsyncioChildComm


logger = logging.getLogger(__name__)


class AppletControlIPC:
    def __init__(self, ipc):
        self.ipc = ipc

    def set_dataset(self, key, value, persist=None):
        self.ipc.set_dataset(key, value, persist)

    def mutate_dataset(self, key, index, value):
        mod = {"action": "setitem", "path": [key, 1], "key": index, "value": value}
        self.ipc.update_dataset(mod)

    def append_to_dataset(self, key, value):
        mod = {"action": "append", "path": [key, 1], "x": value}
        self.ipc.update_dataset(mod)


class AppletControlRPC:
    def __init__(self, loop, dataset_ctl):
        self.loop = loop
        self.dataset_ctl = dataset_ctl
        self.background_tasks = set()

    def _background(self, coro, *args):
        task = self.loop.create_task(coro(*args))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def set_dataset(self, key, value, persist=None):
        self._background(self.dataset_ctl.set, key, value, persist)

    def mutate_dataset(self, key, index, value):
        mod = {"action": "setitem", "path": [key, 1], "key": index, "value": value}
        self._background(self.dataset_ctl.update, mod)

    def append_to_dataset(self, key, value):
        mod = {"action": "append", "path": [key, 1], "x": value}
        self._background(self.dataset_ctl.update, mod)


class AppletIPCClient(AsyncioChildComm):
    def set_close_cb(self, close_cb):
        self.close_cb = close_cb

    def write_pyon(self, obj):
        self.write(pyon.encode(obj).encode() + b"\n")

    async def read_pyon(self):
        line = await self.readline()
        return pyon.decode(line.decode())

    async def embed(self, win_id):
        # This function is only called when not subscribed to anything,
        # so the only normal replies are embed_done and terminate.
        self.write_pyon({"action": "embed",
                         "win_id": win_id})
        reply = await self.read_pyon()
        if reply["action"] == "terminate":
            self.close_cb()
        elif reply["action"] != "embed_done":
            logger.error("unexpected action reply to embed request: %s",
                         reply["action"])
            self.close_cb()

    def fix_initial_size(self):
        self.write_pyon({"action": "fix_initial_size"})

    async def listen(self):
        data = None
        while True:
            obj = await self.read_pyon()
            try:
                action = obj["action"]
                if action == "terminate":
                    self.close_cb()
                    return
                elif action == "mod":
                    mod = obj["mod"]
                    if mod["action"] == "init":
                        data = self.init_cb(mod["struct"])
                    else:
                        process_mod(data, mod)
                    self.mod_cb(mod)
                else:
                    raise ValueError("unknown action in parent message")
            except:
                logger.error("error processing parent message",
                             exc_info=True)
                self.close_cb()

    def subscribe(self, datasets, init_cb, mod_cb, dataset_prefixes=[], *, loop):
        self.write_pyon({"action": "subscribe",
                         "datasets": datasets,
                         "dataset_prefixes": dataset_prefixes})
        self.init_cb = init_cb
        self.mod_cb = mod_cb
        self.listen_task = loop.create_task(self.listen())

    def set_dataset(self, key, value, persist=None):
        self.write_pyon({"action": "set_dataset",
                         "key": key,
                         "value": value,
                         "persist": persist})

    def update_dataset(self, mod):
        self.write_pyon({"action": "update_dataset",
                         "mod": mod})


class SimpleApplet:
    def __init__(self, main_widget_class, cmd_description=None,
                 default_update_delay=0.0):
        self.main_widget_class = main_widget_class

        self.argparser = argparse.ArgumentParser(description=cmd_description)

        self.argparser.add_argument(
            "--update-delay", type=float, default=default_update_delay,
            help="time to wait after a mod (buffering other mods) "
                 "before updating (default: %(default).2f)")

        group = self.argparser.add_argument_group("standalone mode (default)")
        group.add_argument(
            "--server", default="::1",
            help="hostname or IP of the master to connect to "
                 "for dataset notifications "
                 "(ignored in embedded mode)")
        group.add_argument(
            "--port-notify", default=3250, type=int,
            help="TCP port to connect to for notifications (ignored in embedded mode)")
        group.add_argument(
            "--port-control", default=3251, type=int,
            help="TCP port to connect to for control (ignored in embedded mode)")

        self._arggroup_datasets = self.argparser.add_argument_group("datasets")

        self.dataset_args = set()

    def add_dataset(self, name, help=None, required=True):
        kwargs = dict()
        if help is not None:
            kwargs["help"] = help
        if required:
            self._arggroup_datasets.add_argument(name, **kwargs)
        else:
            self._arggroup_datasets.add_argument("--" + name, **kwargs)
        self.dataset_args.add(name)

    def args_init(self):
        self.args = self.argparser.parse_args()
        self.embed = os.getenv("ARTIQ_APPLET_EMBED")
        self.datasets = {getattr(self.args, arg.replace("-", "_"))
                         for arg in self.dataset_args}
        # Optional prefixes (dataset sub-trees) to match subscriptions against;
        # currently only used by out-of-tree subclasses (ndscan).
        self.dataset_prefixes = []

    def qasync_init(self):
        app = QtWidgets.QApplication([])
        self.loop = QEventLoop(app)
        asyncio.set_event_loop(self.loop)

    def ipc_init(self):
        if self.embed is not None:
            self.ipc = AppletIPCClient(self.embed)
            self.loop.run_until_complete(self.ipc.connect())

    def ipc_close(self):
        if self.embed is not None:
            self.ipc.close()

    def ctl_init(self):
        if self.embed is None:
            dataset_ctl = RPCClient()
            self.loop.run_until_complete(dataset_ctl.connect_rpc(
                self.args.server, self.args.port_control, "master_dataset_db"))
            self.ctl = AppletControlRPC(self.loop, dataset_ctl)
        else:
            self.ctl = AppletControlIPC(self.ipc)

    def ctl_close(self):
        if self.embed is None:
            self.ctl.dataset_ctl.close_rpc()

    def create_main_widget(self):
        self.main_widget = self.main_widget_class(self.args, self.ctl)
        if self.embed is not None:
            self.ipc.set_close_cb(self.main_widget.close)
            if os.name == "nt":
                # HACK: if the window has a frame, there will be garbage
                # (usually white) displayed at its right and bottom borders
                #  after it is embedded.
                self.main_widget.setWindowFlags(QtCore.Qt.FramelessWindowHint)
                self.main_widget.show()
                win_id = int(self.main_widget.winId())
                self.loop.run_until_complete(self.ipc.embed(win_id))
            else:
                # HACK:
                # Qt window embedding is ridiculously buggy, and empirical
                # testing has shown that the following procedure must be
                # followed exactly on Linux:
                # 1. applet creates widget
                # 2. applet creates native window without showing it, and
                #    gets its ID
                # 3. applet sends the ID to host, host embeds the widget
                # 4. applet shows the widget
                # 5. parent resizes the widget
                win_id = int(self.main_widget.winId())
                self.loop.run_until_complete(self.ipc.embed(win_id))
                self.main_widget.show()
                self.ipc.fix_initial_size()
        else:
            self.main_widget.show()

    def sub_init(self, data):
        self.data = data
        return data

    def is_dataset_subscribed(self, key):
        if key in self.datasets:
            return True
        for prefix in self.dataset_prefixes:
            if key.startswith(prefix):
                return True
        return False

    def filter_mod(self, mod):
        if self.embed is not None:
            # the parent already filters for us
            return True

        if mod["action"] == "init":
            return True
        if mod["path"]:
            return self.is_dataset_subscribed(mod["path"][0])
        elif mod["action"] in {"setitem", "delitem"}:
            return self.is_dataset_subscribed(mod["key"])
        else:
            return False

    def emit_data_changed(self, data, mod_buffer):
        self.main_widget.data_changed(data, mod_buffer)

    def flush_mod_buffer(self):
        self.emit_data_changed(self.data, self.mod_buffer)
        del self.mod_buffer

    def sub_mod(self, mod):
        if not self.filter_mod(mod):
            return

        if self.args.update_delay:
            if hasattr(self, "mod_buffer"):
                self.mod_buffer.append(mod)
            else:
                self.mod_buffer = [mod]
                self.loop.call_later(self.args.update_delay,
                                     self.flush_mod_buffer)
        else:
            self.emit_data_changed(self.data, [mod])

    def subscribe(self):
        if self.embed is None:
            self.subscriber = Subscriber("datasets",
                                         self.sub_init, self.sub_mod)
            self.loop.run_until_complete(self.subscriber.connect(
                self.args.server, self.args.port_notify))
        else:
            self.ipc.subscribe(self.datasets, self.sub_init, self.sub_mod,
                               dataset_prefixes=self.dataset_prefixes,
                               loop=self.loop)

    def unsubscribe(self):
        if self.embed is None:
            self.loop.run_until_complete(self.subscriber.close())

    def run(self):
        self.args_init()
        self.qasync_init()
        try:
            self.ipc_init()
            try:
                self.ctl_init()
                try:
                    self.create_main_widget()
                    self.subscribe()
                    try:
                        self.loop.run_forever()
                    finally:
                        self.unsubscribe()
                finally:
                    self.ctl_close()
            finally:
                self.ipc_close()
        finally:
            self.loop.close()


class TitleApplet(SimpleApplet):
    def __init__(self, *args, **kwargs):
        SimpleApplet.__init__(self, *args, **kwargs)
        self.argparser.add_argument("--title", default=None,
                                    help="set title (can be a Python format "
                                    "string where field names are dataset "
                                    "names, replace '.' with '/')")

    def args_init(self):
        SimpleApplet.args_init(self)
        if self.args.title is not None:
            self.dataset_title = set()
            parsed = string.Formatter().parse(self.args.title)
            for _, format_field, _, _ in parsed:
                if format_field is None:
                    break
                if not format_field:
                    raise ValueError("Invalid title format string")
                self.dataset_title.add(format_field.replace("/", "."))
            self.datasets |= self.dataset_title

    def emit_data_changed(self, data, mod_buffer):
        if self.args.title is not None:
            title_values = {k.replace(".", "/"): data.get(k, (False, None))[1]
                            for k in self.dataset_title}
            try:
                title = self.args.title.format(**title_values)
            except:
                logger.warning("failed to format title", exc_info=True)
                title = self.args.title
        else:
            title = None
        self.main_widget.data_changed(data, mod_buffer, title)
