"""JDK requirement parsers and the detection comment / JAVA_HOME env."""

from pathlib import Path

import pytest
import yaml

from crucible.mine import config, detect, jdk

POM = """<project>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>"""


@pytest.mark.parametrize("text,expected", [
    (POM, 8),
    ("<properties><java.version>17</java.version></properties>", 17),
    ("<properties><maven.compiler.release>21</maven.compiler.release></properties>", 21),
    (("<properties><maven.compiler.release>21</maven.compiler.release>"
     "<java.version>11</java.version></properties>"), 21),
    (("<properties><java.version>11</java.version><maven.compiler.source>${java.version}"
     "</maven.compiler.source></properties>"), 11),
    (("<plugin><artifactId>maven-compiler-plugin</artifactId><configuration>"
     "<release>17</release></configuration></plugin>"), 17),
    (("<plugin><artifactId>maven-compiler-plugin</artifactId><configuration>"
     "<source>1.7</source><target>1.7</target></configuration></plugin>"), 7),
    ("<project/>", None),
    ("<properties><java.version>${undefined}</java.version></properties>", None),
])
def test_parse_pom(text, expected):
    assert jdk.parse_pom(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }", 21),
    ("java { toolchain { languageVersion.set(JavaLanguageVersion.of(17)) } }", 17),
    ("kotlin { jvmToolchain(11) }", 11),
    ("kotlin { jvmToolchain { languageVersion.set(JavaLanguageVersion.of(8)) } }", 8),
    ("sourceCompatibility = '1.8'", 8),
    ("sourceCompatibility = JavaVersion.VERSION_17", 17),
    ("sourceCompatibility = JavaVersion.VERSION_1_8", 8),
    ("languageVersion = 21", 21),
    ("plugins { id 'java' }", None),
])
def test_parse_gradle(text, expected):
    assert jdk.parse_gradle(text) == expected


def test_parse_gradle_takes_the_highest_when_several():
    assert jdk.parse_gradle("jvmToolchain(11)\nlanguageVersion = JavaLanguageVersion.of(17)") == 17


@pytest.mark.parametrize("text,expected", [
    ('[versions]\njdk = "17"\nkotlin = "1.9.0"\n', 17),
    ('[versions]\njava = "21"\n', 21),
    ('[versions]\njavaVersion = "11"\n', 11),
    ('[versions]\nkotlin = "1.9.0"\n', None),
])
def test_parse_version_catalog(text, expected):
    assert jdk.parse_catalog(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("- uses: actions/setup-java@v4\n  with:\n    distribution: temurin\n    java-version: 17\n", 17),
    ("- uses: actions/setup-java@v4\n  with:\n    java-version: '21'\n", 21),
    ('    java-version: "1.8"\n', 8),
    ("    java-version: ${{ matrix.java }}\n", None),
    ("run: echo hi\n", None),
])
def test_parse_ci(text, expected):
    assert jdk.parse_ci(text) == expected


def test_required_from_a_gradle_project_reads_catalog_and_subprojects(tmp_path):
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text('[versions]\njdk = "17"\n')
    (tmp_path / "build.gradle.kts").write_text("plugins { java }\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "build.gradle.kts").write_text("kotlin { jvmToolchain(21) }\n")
    req = jdk.required_java(tmp_path, tmp_path, "gradle")
    assert req and req.version == 21 and "app/build.gradle.kts" in req.source


def test_required_falls_back_to_ci(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("steps:\n  - uses: actions/setup-java@v4\n    with:\n"
                               "      java-version: 17\n")
    req = jdk.required_java(tmp_path, tmp_path, "maven")
    assert req and req.version == 17 and "ci.yml" in req.source


def test_required_none_when_nothing_says(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert jdk.required_java(tmp_path, tmp_path, "maven") is None


def test_local_lookup_from_env_vars():
    env = {"JAVA_HOME_17_X64": "/opt/jdk17", "JAVA_HOME_21_ARM64": "/opt/jdk21"}
    assert jdk.find_local_jdk(17, env=env, java_home_tool=None, jvm_dir=None) == "/opt/jdk17"
    assert jdk.find_local_jdk(21, env=env, java_home_tool=None, jvm_dir=None) == "/opt/jdk21"
    assert jdk.find_local_jdk(8, env=env, java_home_tool=None, jvm_dir=None) is None


def test_local_lookup_linux_jvm_dir(tmp_path):
    for n in ("java-17-openjdk-amd64", "java-1.8.0-openjdk", "temurin-21-jdk", "java-11-openjdk"):
        (tmp_path / n).mkdir()
    find = lambda v: jdk.find_local_jdk(v, env={}, java_home_tool=None, jvm_dir=tmp_path)
    assert find(17) == str(tmp_path / "java-17-openjdk-amd64")
    assert find(8) == str(tmp_path / "java-1.8.0-openjdk")
    assert find(21) == str(tmp_path / "temurin-21-jdk")
    assert find(1) is None and find(7) is None


def test_local_lookup_macos_java_home_tool(tmp_path):
    tool = tmp_path / "java_home"
    tool.write_text('#!/bin/sh\n[ "$2" = 17 ] && echo /Library/Java/jdk-17 && exit 0\nexit 1\n')
    tool.chmod(0o755)
    assert jdk.find_local_jdk(17, env={}, java_home_tool=str(tool), jvm_dir=None) \
        == "/Library/Java/jdk-17"
    assert jdk.find_local_jdk(11, env={}, java_home_tool=str(tool), jvm_dir=None) is None


def write_maven(root: Path, version="17"):
    (root / "pom.xml").write_text(
        f"<project><properties><maven.compiler.release>{version}</maven.compiler.release>"
        "</properties></project>")
    t = root / "src" / "test" / "java"
    t.mkdir(parents=True)
    (t / "FooTest.java").write_text("class FooTest {}")


def test_detect_sets_java_home_when_a_jdk_is_found(tmp_path):
    write_maven(tmp_path)
    [d] = detect.detect(tmp_path, jdk_lookup=lambda v: f"/jdks/{v}")
    assert d.suite.env == {"JAVA_HOME": "/jdks/17"}
    text = "\n".join(d.comment())
    assert "Java 17" in text and "pom.xml" in text


def test_detect_comments_a_container_wrapper_when_no_jdk(tmp_path):
    write_maven(tmp_path)
    [d] = detect.detect(tmp_path, jdk_lookup=lambda v: None)
    assert d.suite.env == {}
    text = "\n".join(d.comment())
    assert "Java 17" in text and "container" in text and ".crucible/scripts" in text


def test_written_mine_yaml_with_java_home_loads(tmp_path):
    write_maven(tmp_path)
    out = tmp_path / "mine.yaml"
    config.write_default(out, tmp_path, jdk_lookup=lambda v: "/Library/Java/JavaVirtualMachines/"
                         "temurin-17.jdk/Contents/Home")
    assert "Java 17" in out.read_text()
    cfg = config.load_config(out)
    assert cfg.suites[0].env["JAVA_HOME"].endswith("Contents/Home")
    assert yaml.safe_load(out.read_text())["suites"][0]["env"]["JAVA_HOME"]


def test_java_home_is_not_secret_like():
    assert not config._secret_like("JAVA_HOME")
