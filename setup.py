"""本地安装入口。"""

from setuptools import find_packages, setup


setup(
    name="battery-logistics-operations",
    version="0.1.0",
    description="储能资产平台调度与能源分析、调度与现场机组巡检分析准入服务",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
