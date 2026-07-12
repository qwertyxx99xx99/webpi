from setuptools import setup


# Installing this tiny package makes sitecustomize importable during Python's
# startup, before the `streamlit` console script constructs its web server.
setup(
    name="webpi-bootstrap",
    version="0.1.0",
    py_modules=["sitecustomize", "webpi_bridge"],
    packages=["pi_extensions", "pi_config"],
    package_data={
        "pi_extensions": ["*.ts"],
        "pi_config": ["*.md"],
    },
    include_package_data=True,
)
