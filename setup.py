from setuptools import setup


# Installing this tiny package makes sitecustomize importable during Python's
# startup, before the `streamlit` console script constructs its web server.
setup(
    name="webpi-bootstrap",
    version="0.9.0",
    py_modules=["sitecustomize", "webpi_bridge"],
    packages=["pi_extensions", "pi_config", "pi_baml"],
    package_data={
        "pi_extensions": ["*.ts"],
        "pi_config": ["*.md"],
        "pi_baml": ["baml_src/*.baml", "baml_client/*.ts"],
    },
    include_package_data=True,
)
