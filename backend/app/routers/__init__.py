"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import borehole as router_borehole
from app.routers import core as router_core
from app.routers import stratigraphy as router_stratigraphy
from app.routers import geophysics as router_geophysics
from app.routers import geochem as router_geochem
from app.routers import assay as router_assay
from app.routers import mapping as router_mapping
from app.routers import survey_point as router_survey_point
from app.routers import drilling_log as router_drilling_log
from app.routers import reserve as router_reserve
from app.routers import sample_registry as router_sample_registry
from app.routers import sample_cycle as router_sample_cycle
from app.routers import equipment as router_equipment
from app.routers import hydro as router_hydro
from app.routers import section as router_section
from app.routers import geological_report as router_geological_report
from app.routers import remote as router_remote
from app.routers import mineral as router_mineral
from app.routers import environmental as router_environmental

ROUTERS = [router_borehole, router_core, router_stratigraphy, router_geophysics, router_geochem, router_assay, router_mapping, router_survey_point, router_drilling_log, router_reserve, router_sample_registry, router_sample_cycle, router_equipment, router_hydro, router_section, router_geological_report, router_remote, router_mineral, router_environmental]
