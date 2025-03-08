from setuptools import setup, find_packages

setup(
    name='pvemon',
    version = "1.3.0",
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'pvemon=pvemon:main',
        ],
    },
)
