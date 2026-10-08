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
    # cyclic property references must not recurse forever
    ("<properties><a>${b}</a><b>${a}</b><java.version>${a}</java.version></properties>", None),
    ("<properties><java.version>${java.version}</java.version></properties>", None),
    # a non-numeric first tag is skipped in favour of a later numeric one
    ("<x><source>true</source></x><plugin><source>11</source></plugin>", 11),
    ("<x><release>${project.basedir}/r</release></x><release>17</release>", 17),
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


def make_jdk(path: Path, version: str) -> Path:
    path.mkdir(parents=True)
    (path / "release").write_text(f'IMPLEMENTOR="x"\nJAVA_VERSION="{version}"\n')
    (path / "bin").mkdir()
    (path / "bin" / "javac").write_text("")
    return path


def find(version, tmp_path, env=None, tool=None):
    return jdk.find_local_jdk(version, env=env or {}, java_home_tool=tool,
                              roots=[str(tmp_path / "*")])


def test_local_lookup_from_env_vars(tmp_path):
    env = {"JAVA_HOME_17_X64": str(make_jdk(tmp_path / "a", "17.0.1")),
           "JAVA_HOME_21_ARM64": str(make_jdk(tmp_path / "b", "21.0.2"))}
    nowhere = [str(tmp_path / "none" / "*")]
    find_env = lambda v: jdk.find_local_jdk(v, env=env, java_home_tool=None, roots=nowhere)
    assert find_env(17) == env["JAVA_HOME_17_X64"]
    assert find_env(21) == env["JAVA_HOME_21_ARM64"]
    assert find_env(22) is None


def test_major_comes_from_the_release_file_not_the_directory_name(tmp_path):
    make_jdk(tmp_path / "jdk-17.0.8", "17.0.8")
    make_jdk(tmp_path / "jdk-11.0.17", "11.0.17")
    assert find(8, tmp_path) == str(tmp_path / "jdk-11.0.17")  # lowest major above 8, not 17.0.8
    assert find(17, tmp_path) == str(tmp_path / "jdk-17.0.8")
    assert find(11, tmp_path) == str(tmp_path / "jdk-11.0.17")
    assert find(18, tmp_path) is None


def test_misleading_directory_names_are_ignored(tmp_path):
    make_jdk(tmp_path / "java-21-openjdk", "17.0.1")  # the name lies
    assert find(21, tmp_path) is None
    assert find(17, tmp_path) == str(tmp_path / "java-21-openjdk")


def test_exact_major_beats_newer_and_lowest_newer_beats_highest(tmp_path):
    make_jdk(tmp_path / "a", "26.0.1")
    make_jdk(tmp_path / "b", "22.0.1")
    make_jdk(tmp_path / "c", "21.0.5")
    assert find(21, tmp_path) == str(tmp_path / "c")
    assert find(17, tmp_path) == str(tmp_path / "c")  # 21 is the lowest major above 17
    (tmp_path / "c" / "release").unlink()
    assert find(17, tmp_path) == str(tmp_path / "b")  # not 26


def test_old_style_version_is_read_as_8(tmp_path):
    make_jdk(tmp_path / "x", "1.8.0_392")
    assert find(8, tmp_path) == str(tmp_path / "x")
    assert find(7, tmp_path) == str(tmp_path / "x")


def test_macos_bundle_layout_is_found(tmp_path):
    make_jdk(tmp_path / "temurin-21.jdk" / "Contents" / "Home", "21.0.3")
    assert find(21, tmp_path) == str(tmp_path / "temurin-21.jdk" / "Contents" / "Home")


def test_java_home_tool_listing_is_parsed(tmp_path):
    h22 = make_jdk(tmp_path / "jvms" / "j22" / "Contents" / "Home", "22.0.1")
    h21 = make_jdk(tmp_path / "jvms" / "j21" / "Contents" / "Home", "21.0.2")
    tool = tmp_path / "java_home"
    tool.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -V ]; then\n'
        '  echo "Matching Java Virtual Machines (2):" >&2\n'
        f'  echo \'    22.0.1 (arm64) "Oracle" - "Java SE 22.0.1" {h22}\' >&2\n'
        f'  echo \'    21.0.2 (arm64) "Eclipse Adoptium" - "OpenJDK 21.0.2" {h21}\' >&2\n'
        "  exit 0\nfi\nexit 1\n")
    tool.chmod(0o755)
    nowhere = [str(tmp_path / "none" / "*")]
    assert jdk.find_local_jdk(21, env={}, java_home_tool=str(tool), roots=nowhere) == str(h21)
    assert jdk.find_local_jdk(17, env={}, java_home_tool=str(tool), roots=nowhere) == str(h21)
    assert jdk.find_local_jdk(23, env={}, java_home_tool=str(tool), roots=nowhere) is None


def test_enumeration_is_injectable_and_dedups_symlinks(tmp_path):
    real = make_jdk(tmp_path / "real", "17.0.1")
    (tmp_path / "current").symlink_to(real)
    found = jdk.local_jdks(env={}, java_home_tool=None, roots=[str(tmp_path / "*")])
    assert [j.major for j in found] == [17]


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


def test_gradle_ignores_buildsrc_and_plugin_builds_for_the_max(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("kotlin { jvmToolchain(17) }\n")
    (tmp_path / "buildSrc").mkdir()
    (tmp_path / "buildSrc" / "build.gradle.kts").write_text("kotlin { jvmToolchain(21) }\n")
    (tmp_path / "build-logic").mkdir()
    (tmp_path / "build-logic" / "build.gradle.kts").write_text("kotlin { jvmToolchain(21) }\n")
    (tmp_path / "gradle-plugin").mkdir()
    (tmp_path / "gradle-plugin" / "build.gradle").write_text("sourceCompatibility = '22'\n")
    req = jdk.required_java(tmp_path, tmp_path, "gradle")
    assert req and req.version == 17 and req.source == "build.gradle.kts"


def test_gradle_falls_back_to_buildsrc_when_nothing_else_says(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("plugins { java }\n")
    (tmp_path / "buildSrc").mkdir()
    (tmp_path / "buildSrc" / "build.gradle.kts").write_text("kotlin { jvmToolchain(11) }\n")
    req = jdk.required_java(tmp_path, tmp_path, "gradle")
    assert req and req.version == 11


def test_unreadable_candidates_never_crash_detection(tmp_path):
    locked = tmp_path / "locked"
    make_jdk(locked / "jdk17", "17.0.9")
    locked.chmod(0)
    try:
        env = {"JAVA_HOME_17_X64": str(locked / "jdk17")}
        assert jdk.local_jdks(env=env, java_home_tool=None, roots=[str(locked / "*")]) == []
    finally:
        locked.chmod(0o755)


def test_a_jre_without_javac_is_not_a_jdk(tmp_path):
    jre = tmp_path / "jre8"
    jre.mkdir()
    (jre / "release").write_text('JAVA_VERSION="1.8.0_411"\n')  # a JRE: no bin/javac
    full = make_jdk(tmp_path / "jdk11", "11.0.20")
    found = jdk.local_jdks(env={}, java_home_tool=None, roots=[str(tmp_path / "*")])
    homes = [str(j.home) if hasattr(j, "home") else str(j) for j in found]
    assert not any(str(jre) in h for h in homes)
    assert jdk.pick_jdk(8, found) is not None and str(full) in str(jdk.pick_jdk(8, found))
