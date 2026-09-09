"""人机验证: 业务异常。"""


class VerificationFailed(Exception):
    """产物运行期: 单次验证触发在限定尝试次数内仍未通过。

    由 run_code 顶层捕获并转换为本次运行终止(镜像 LoginCancelled 的处理方式)。
    """

    def __init__(self, message: str, *, type: str = "", attempts: int = 0,
                 reason: str = ""):
        super().__init__(message)
        self.verify_type = type
        self.attempts = attempts
        self.reason = reason


class VerificationCancelled(Exception):
    """Agent 开发期 HITL: 用户取消验证求助(镜像 LoginCancelled)。"""
