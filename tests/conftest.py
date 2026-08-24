"""pytest 全局夹具: 测试进程关闭控制面接入(客户端登录走本地校验)。

真机部署不受影响(该 env 仅 pytest 进程设置); 控制面在 tests/test_console*.py
与 tests/test_client_registry.py 中以 mock/独立库方式单独覆盖。
"""
import os

os.environ.setdefault("CREATORHUB_CONSOLE_OFF", "1")
# 排除"测试旁路"误入真实控制面联调
os.environ.pop("CREATORHUB_CONSOLE_LIVE", None)