from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="webx11",
    version="0.1.0",
    author="lp1",
    author_email="webx11@lp1.eu",
    description="Stream GNU/Linux GUI applications to web browsers via WebTransport/WebSocket",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/lp1dev/WebX11",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Topic :: System :: Systems Administration",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: POSIX :: Linux",
    ],
    python_requires=">=3.8",
    install_requires=[
        "Pillow>=9.0.0",
        "python-xlib>=0.31",
        "websockets>=10.0",
    ],
    extras_require={
        "webtransport": ["aioquic>=0.9.0"],
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.18.0",
            "black>=22.0.0",
            "flake8>=4.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "webx11=webx11.main:main",
        ],
    },
    package_data={
        "webx11": [
            "partials/*.html",
            "settings.json",
        ],
    },
    include_package_data=True,
    project_urls={
        "Bug Reports": "https://github.com/lp1dev/WebX11/issues",
        "Source": "https://github.com/lp1dev/WebX11",
        "Documentation": "https://github.com/lp1dev/WebX11",
    },
    keywords="x11 xvfb webtransport websocket streaming remote-desktop",
)
