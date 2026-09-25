"""联合教研议题决议的服务端包入口。"""
PROJECT_CODE = "service_09252_009"

def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "联合教研议题决议"}
