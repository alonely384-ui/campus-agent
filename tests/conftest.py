"""pytest 全局夹具。

这里只做一件事：把测试环境的默认值调成「不会因为测试跑太快而自伤」。

为什么要专门写这个文件：
    安全层加进来之后，一个 pytest 进程在几十秒内会对同一个模板打上几十次请求
    （upload 的 6 个用例、chat 的各类参数校验、reader 的多次翻页……）。
    那些在真实世界里属于「压测」，会正常触发令牌桶 429，
    于是测试从「验证功能」变成「验证限流器工作正常」，掩盖了真正要测的东西。

    所以测试里默认关限流（SECURITY_LIMIT_ENABLED=0），
    而限流器本身由 test_security.py 单独、显式地测——
    测限流的时候我们主动构造超限场景，不依赖别的用例留下的烂摊子。

注意：这里关掉的只是**限流**。认证、路由对账、请求体上限全部照常生效，
它们不依赖请求频率，跑多少次结果都一样。
"""

import os

# 必须在任何 app.* 模块被 import 之前设置：
# app.config 的 Settings 是模块级单例，import 的那一刻就把环境变量读进去了，
# 事后再 os.environ[...] = ... 对已经构造好的 settings 无效。
os.environ.setdefault("SECURITY_LIMIT_ENABLED", "0")
os.environ.setdefault("APP_ENV", "dev")