# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import enum
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, NamedTuple, Optional

import fasteners

from compiler_gym.datasets import Benchmark, TarDatasetWithManifest
from compiler_gym.service.proto import BenchmarkDynamicConfig, Command
from compiler_gym.third_party import llvm
from compiler_gym.util.download import download
from compiler_gym.util.runfiles_path import cache_path, site_data_path
from compiler_gym.util.timer import Timer
from compiler_gym.validation_result import ValidationError

logger = logging.getLogger(__name__)

_CBENCH_TARS = {
    "macos": (
        "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cBench-v1-macos.tar.bz2",
        "90b312b40317d9ee9ed09b4b57d378879f05e8970bb6de80dc8581ad0e36c84f",
    ),
    "linux": (
        "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cBench-v1-linux.tar.bz2",
        "601fff3944c866f6617e653b6eb5c1521382c935f56ca1f36a9f5cf1a49f3de5",
    ),
}

_CBENCH_RUNTOME_DATA = (
    "https://dl.fbaipublicfiles.com/compiler_gym/cBench-v0-runtime-data.tar.bz2",
    "a1b5b5d6b115e5809ccaefc2134434494271d184da67e2ee43d7f84d07329055",
)


if sys.platform == "darwin":
    _COMPILE_ARGS = [
        "-L",
        "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/lib",
    ]
else:
    _COMPILE_ARGS = []


class LlvmSanitizer(enum.IntEnum):
    """The LLVM sanitizers."""

    ASAN = 1
    TSAN = 2
    MSAN = 3
    UBSAN = 4


# Compiler flags that are enabled by sanitizers.
_SANITIZER_FLAGS = {
    LlvmSanitizer.ASAN: ["-O1", "-g", "-fsanitize=address", "-fno-omit-frame-pointer"],
    LlvmSanitizer.TSAN: ["-O1", "-g", "-fsanitize=thread"],
    LlvmSanitizer.MSAN: ["-O1", "-g", "-fsanitize=memory"],
    LlvmSanitizer.UBSAN: ["-fsanitize=undefined"],
}


class BenchmarkExecutionResult(NamedTuple):
    """The result of running a benchmark."""

    walltime_seconds: float
    """The execution time in seconds."""

    error: Optional[ValidationError] = None
    """An error."""

    output: Optional[str] = None
    """The output generated by the benchmark."""

    def json(self):
        return self._asdict()  # pylint: disable=no-member


def _compile_and_run_bitcode_file(
    bitcode_file: Path,
    cmd: str,
    cwd: Path,
    linkopts: List[str],
    env: Dict[str, str],
    num_runs: int,
    sanitizer: Optional[LlvmSanitizer] = None,
    timeout_seconds: float = 300,
    compilation_timeout_seconds: float = 60,
) -> BenchmarkExecutionResult:
    """Run the given cBench benchmark."""
    # cBench benchmarks expect that a file _finfo_dataset exists in the
    # current working directory and contains the number of benchmark
    # iterations in it.
    with open(cwd / "_finfo_dataset", "w") as f:
        print(num_runs, file=f)

    # Create a barebones execution environment for the benchmark.
    run_env = {
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        # Disable all logging from GRPC. In the past I have had false-positive
        # "Wrong output" errors caused by GRPC error messages being logged to
        # stderr.
        "GRPC_VERBOSITY": "NONE",
    }
    run_env.update(env)

    error_data = {}

    if sanitizer:
        clang_path = llvm.clang_path()
        binary = cwd / "a.out"
        error_data["run_cmd"] = cmd.replace("$BIN", "./a.out")
        # Generate the a.out binary file.
        compile_cmd = (
            [clang_path.name, str(bitcode_file), "-o", str(binary)]
            + _COMPILE_ARGS
            + list(linkopts)
            + _SANITIZER_FLAGS.get(sanitizer, [])
        )
        error_data["compile_cmd"] = compile_cmd
        logger.debug("compile: %s", compile_cmd)
        assert not binary.is_file()
        clang = subprocess.Popen(
            compile_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env={"PATH": f"{clang_path.parent}:{os.environ.get('PATH', '')}"},
        )
        try:
            output, _ = clang.communicate(timeout=compilation_timeout_seconds)
        except subprocess.TimeoutExpired:
            # kill() was added in Python 3.7.
            if sys.version_info >= (3, 7, 0):
                clang.kill()
            else:
                clang.terminate()
            clang.communicate(timeout=30)  # Wait for shutdown to complete.
            error_data["timeout"] = compilation_timeout_seconds
            return BenchmarkExecutionResult(
                walltime_seconds=timeout_seconds,
                error=ValidationError(
                    type="Compilation timeout",
                    data=error_data,
                ),
            )
        if clang.returncode:
            error_data["output"] = output
            return BenchmarkExecutionResult(
                walltime_seconds=timeout_seconds,
                error=ValidationError(
                    type="Compilation failed",
                    data=error_data,
                ),
            )
        assert binary.is_file()
    else:
        lli_path = llvm.lli_path()
        error_data["run_cmd"] = cmd.replace("$BIN", f"{lli_path.name} benchmark.bc")
        run_env["PATH"] = str(lli_path.parent)

    try:
        logger.debug("exec: %s", error_data["run_cmd"])
        process = subprocess.Popen(
            error_data["run_cmd"],
            shell=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            env=run_env,
            cwd=cwd,
        )

        with Timer() as timer:
            stdout, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # kill() was added in Python 3.7.
        if sys.version_info >= (3, 7, 0):
            process.kill()
        else:
            process.terminate()
        process.communicate(timeout=30)  # Wait for shutdown to complete.
        error_data["timeout_seconds"] = timeout_seconds
        return BenchmarkExecutionResult(
            walltime_seconds=timeout_seconds,
            error=ValidationError(
                type="Execution timeout",
                data=error_data,
            ),
        )
    finally:
        if sanitizer:
            binary.unlink()

    try:
        output = stdout.decode("utf-8")
    except UnicodeDecodeError:
        output = "<binary>"

    if process.returncode:
        # Runtime error.
        if sanitizer == LlvmSanitizer.ASAN and "LeakSanitizer" in output:
            error_type = "Memory leak"
        elif sanitizer == LlvmSanitizer.ASAN and "AddressSanitizer" in output:
            error_type = "Memory error"
        elif sanitizer == LlvmSanitizer.MSAN and "MemorySanitizer" in output:
            error_type = "Memory error"
        elif "Segmentation fault" in output:
            error_type = "Segmentation fault"
        elif "Illegal Instruction" in output:
            error_type = "Illegal Instruction"
        else:
            error_type = f"Runtime error ({process.returncode})"

        error_data["return_code"] = process.returncode
        error_data["output"] = output
        return BenchmarkExecutionResult(
            walltime_seconds=timer.time,
            error=ValidationError(
                type=error_type,
                data=error_data,
            ),
        )
    return BenchmarkExecutionResult(walltime_seconds=timer.time, output=output)


def download_cBench_runtime_data() -> bool:
    """Download and unpack the cBench runtime dataset."""
    cbench_data = site_data_path("llvm-v0/cbench-v1-runtime-data/runtime_data")
    if (cbench_data / "unpacked").is_file():
        return False
    else:
        # Clean up any partially-extracted data directory.
        if cbench_data.is_dir():
            shutil.rmtree(cbench_data)

        url, sha256 = _CBENCH_RUNTOME_DATA
        tar_contents = io.BytesIO(download(url, sha256))
        with tarfile.open(fileobj=tar_contents, mode="r:bz2") as tar:
            cbench_data.parent.mkdir(parents=True, exist_ok=True)
            tar.extractall(cbench_data.parent)
        assert cbench_data.is_dir()
        # Create the marker file to indicate that the directory is unpacked
        # and ready to go.
        (cbench_data / "unpacked").touch()
        return True


# Thread lock to prevent race on download_cBench_runtime_data() from
# multi-threading. This works in tandem with the inter-process file lock - both
# are required.
_CBENCH_DOWNLOAD_THREAD_LOCK = Lock()


def _make_cBench_validator(
    cmd: str,
    linkopts: List[str],
    os_env: Dict[str, str],
    num_runs: int = 1,
    compare_output: bool = True,
    input_files: Optional[List[Path]] = None,
    output_files: Optional[List[Path]] = None,
    validate_result: Optional[
        Callable[[BenchmarkExecutionResult], Optional[str]]
    ] = None,
    pre_execution_callback: Optional[Callable[[Path], None]] = None,
    sanitizer: Optional[LlvmSanitizer] = None,
    flakiness: int = 5,
) -> Callable[["LlvmEnv"], Optional[ValidationError]]:  # noqa: F821
    """Construct a validation callback for a cBench benchmark. See validator() for usage."""
    input_files = input_files or []
    output_files = output_files or []

    def validator_cb(env: "LlvmEnv") -> Optional[ValidationError]:  # noqa: F821
        """The validation callback."""
        with _CBENCH_DOWNLOAD_THREAD_LOCK:
            with fasteners.InterProcessLock(cache_path(".cbench-v1-runtime-data.LOCK")):
                download_cBench_runtime_data()

        cbench_data = site_data_path("llvm-v0/cbench-v1-runtime-data/runtime_data")
        for input_file_name in input_files:
            path = cbench_data / input_file_name
            if not path.is_file():
                raise FileNotFoundError(f"Required benchmark input not found: {path}")

        # Create a temporary working directory to execute the benchmark in.
        with tempfile.TemporaryDirectory(dir=env.service.connection.working_dir) as d:
            cwd = Path(d)

            # Expand shell variable substitutions in the benchmark command.
            expanded_command = cmd.replace("$D", str(cbench_data))

            # Translate the output file names into paths inside the working
            # directory.
            output_paths = [cwd / o for o in output_files]

            if pre_execution_callback:
                pre_execution_callback(cwd)

            # Produce a gold-standard output using a reference version of
            # the benchmark.
            if compare_output or output_files:
                gs_env = env.fork()
                try:
                    # Reset to the original benchmark state and compile it.
                    gs_env.reset(benchmark=env.benchmark)
                    gs_env.write_bitcode(cwd / "benchmark.bc")
                    gold_standard = _compile_and_run_bitcode_file(
                        bitcode_file=cwd / "benchmark.bc",
                        cmd=expanded_command,
                        cwd=cwd,
                        num_runs=1,
                        # Use default optimizations for gold standard.
                        linkopts=linkopts + ["-O2"],
                        # Always assume safe.
                        sanitizer=None,
                        env=os_env,
                    )
                    if gold_standard.error:
                        return ValidationError(
                            type=f"Gold standard: {gold_standard.error.type}",
                            data=gold_standard.error.data,
                        )
                finally:
                    gs_env.close()

                # Check that the reference run produced the expected output
                # files.
                for path in output_paths:
                    if not path.is_file():
                        try:
                            output = gold_standard.output
                        except UnicodeDecodeError:
                            output = "<binary>"
                        raise FileNotFoundError(
                            f"Expected file '{path.name}' not generated\n"
                            f"Benchmark: {env.benchmark}\n"
                            f"Command: {cmd}\n"
                            f"Output: {output}"
                        )
                    path.rename(f"{path}.gold_standard")

            # Serialize the benchmark to a bitcode file that will then be
            # compiled to a binary.
            env.write_bitcode(cwd / "benchmark.bc")
            outcome = _compile_and_run_bitcode_file(
                bitcode_file=cwd / "benchmark.bc",
                cmd=expanded_command,
                cwd=cwd,
                num_runs=num_runs,
                linkopts=linkopts,
                sanitizer=sanitizer,
                env=os_env,
            )

            if outcome.error:
                return outcome.error

            # Run a user-specified validation hook.
            if validate_result:
                validate_result(outcome)

            # Difftest the console output.
            if compare_output and gold_standard.output != outcome.output:
                return ValidationError(
                    type="Wrong output",
                    data={"expected": gold_standard.output, "actual": outcome.output},
                )

            # Difftest the output files.
            for path in output_paths:
                if not path.is_file():
                    return ValidationError(
                        type="Output not generated",
                        data={"path": path.name, "command": cmd},
                    )
                diff = subprocess.Popen(
                    ["diff", str(path), f"{path}.gold_standard"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                stdout, _ = diff.communicate()
                if diff.returncode:
                    try:
                        stdout = stdout.decode("utf-8")
                        return ValidationError(
                            type="Wrong output (file)",
                            data={"path": path.name, "diff": stdout},
                        )
                    except UnicodeDecodeError:
                        return ValidationError(
                            type="Wrong output (file)",
                            data={"path": path.name, "diff": "<binary>"},
                        )

    def flaky_wrapped_cb(env: "LlvmEnv") -> Optional[ValidationError]:  # noqa: F821
        """Wrap the validation callback in a flakiness retry loop."""
        for j in range(1, max(flakiness, 1) + 1):
            try:
                error = validator_cb(env)
                if not error:
                    return
            except TimeoutError:
                # Timeout errors can be raised by the environment in case of a
                # slow step / observation, and should be retried.
                pass
            logger.warning("Validation callback failed, attempt=%d/%d", j, flakiness)
        return error

    return flaky_wrapped_cb


def validator(
    benchmark: str,
    cmd: str,
    data: Optional[List[str]] = None,
    outs: Optional[List[str]] = None,
    platforms: Optional[List[str]] = None,
    compare_output: bool = True,
    validate_result: Optional[
        Callable[[BenchmarkExecutionResult], Optional[str]]
    ] = None,
    linkopts: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    pre_execution_callback: Optional[Callable[[], None]] = None,
    sanitizers: Optional[List[LlvmSanitizer]] = None,
) -> bool:
    """Declare a new benchmark validator.

    TODO(cummins): Pull this out into a public API.

    :param benchmark: The name of the benchmark that this validator supports.
    :cmd: The shell command to run the validation. Variable substitution is
        applied to this value as follows: :code:`$BIN` is replaced by the path
        of the compiled binary and :code:`$D` is replaced with the path to the
        benchmark's runtime data directory.
    :data: A list of paths to input files.
    :outs: A list of paths to output files.
    :return: :code:`True` if the new validator was registered, else :code:`False`.
    """
    platforms = platforms or ["linux", "macos"]
    if {"darwin": "macos"}.get(sys.platform, sys.platform) not in platforms:
        return False
    infiles = data or []
    outfiles = [Path(p) for p in outs or []]
    linkopts = linkopts or []
    env = env or {}
    if sanitizers is None:
        sanitizers = LlvmSanitizer

    VALIDATORS[benchmark].append(
        _make_cBench_validator(
            cmd=cmd,
            input_files=infiles,
            output_files=outfiles,
            compare_output=compare_output,
            validate_result=validate_result,
            linkopts=linkopts,
            os_env=env,
            pre_execution_callback=pre_execution_callback,
        )
    )

    # Register additional validators using the sanitizers.
    if sys.platform.startswith("linux"):
        for sanitizer in sanitizers:
            VALIDATORS[benchmark].append(
                _make_cBench_validator(
                    cmd=cmd,
                    input_files=infiles,
                    output_files=outfiles,
                    compare_output=compare_output,
                    validate_result=validate_result,
                    linkopts=linkopts,
                    os_env=env,
                    pre_execution_callback=pre_execution_callback,
                    sanitizer=sanitizer,
                )
            )

    # Create the BenchmarkDynamicConfig object.
    cbench_data = site_data_path("llvm-v0/cbench-v1-runtime-data/runtime_data")
    DYNAMIC_CONFIGS[benchmark] = BenchmarkDynamicConfig(
        build_cmd=Command(
            argument=["$CC", "$IN"] + linkopts,
            timeout_seconds=60,
            outfile=["a.out"],
        ),
        run_cmd=Command(
            argument=cmd.replace("$BIN", "./a.out")
            .replace("$D", str(cbench_data))
            .split(),
            timeout_seconds=300,
            infile=["a.out", "_finfo_dataset"],
            outfile=[str(s) for s in outfiles],
        ),
        pre_run_cmd=[
            Command(argument=["echo", "1", ">_finfo_dataset"], timeout_seconds=30),
        ],
    )

    return True


class CBenchBenchmark(Benchmark):
    """A cBench benchmmark."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for val in VALIDATORS.get(self.uri, []):
            self.add_validation_callback(val)
        self.proto.dynamic_config.MergeFrom(
            DYNAMIC_CONFIGS.get(self.uri, BenchmarkDynamicConfig())
        )


class CBenchDataset(TarDatasetWithManifest):
    def __init__(self, site_data_base: Path):
        platform = {"darwin": "macos"}.get(sys.platform, sys.platform)
        url, sha256 = _CBENCH_TARS[platform]
        super().__init__(
            name="benchmark://cbench-v1",
            description="Runnable C benchmarks",
            license="BSD 3-Clause",
            references={
                "Paper": "https://arxiv.org/pdf/1407.3487.pdf",
                "Homepage": "https://ctuning.org/wiki/index.php/CTools:CBench",
            },
            tar_urls=[url],
            tar_sha256=sha256,
            manifest_urls=[
                "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cbench-v1-manifest.bz2"
            ],
            manifest_sha256="eeffd7593aeb696a160fd22e6b0c382198a65d0918b8440253ea458cfe927741",
            strip_prefix="cBench-v1",
            benchmark_file_suffix=".bc",
            benchmark_class=CBenchBenchmark,
            site_data_base=site_data_base,
            sort_order=-1,
            validatable="Partially",
        )

    def install(self):
        super().install()
        with _CBENCH_DOWNLOAD_THREAD_LOCK:
            with fasteners.InterProcessLock(cache_path(".cbench-v1-runtime-data.LOCK")):
                download_cBench_runtime_data()


class CBenchLegacyDataset2(TarDatasetWithManifest):
    def __init__(
        self,
        site_data_base: Path,
        sort_order: int = 0,
        name="benchmark://cbench-v1",
        manifest_url="https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cbench-v1-manifest.bz2",
        manifest_sha256="eeffd7593aeb696a160fd22e6b0c382198a65d0918b8440253ea458cfe927741",
        deprecated=None,
    ):
        platform = {"darwin": "macos"}.get(sys.platform, sys.platform)
        url, sha256 = _CBENCH_TARS[platform]
        super().__init__(
            name=name,
            description="Runnable C benchmarks",
            license="BSD 3-Clause",
            references={
                "Paper": "https://arxiv.org/pdf/1407.3487.pdf",
                "Homepage": "https://ctuning.org/wiki/index.php/CTools:CBench",
            },
            tar_urls=[url],
            tar_sha256=sha256,
            manifest_urls=[manifest_url],
            manifest_sha256=manifest_sha256,
            strip_prefix="cBench-v1",
            benchmark_file_suffix=".bc",
            site_data_base=site_data_base,
            sort_order=sort_order,
            benchmark_class=CBenchBenchmark,
            deprecated=deprecated,
            validatable="Partially",
        )


# URLs of the deprecated cBench datasets.
_CBENCH_LEGACY_TARS = {
    "macos": (
        "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cBench-v0-macos.tar.bz2",
        "072a730c86144a07bba948c49afe543e4f06351f1cb17f7de77f91d5c1a1b120",
    ),
    "linux": (
        "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cBench-v0-linux.tar.bz2",
        "9b5838a90895579aab3b9375e8eeb3ed2ae58e0ad354fec7eb4f8b31ecb4a360",
    ),
}


class CBenchLegacyDataset(TarDatasetWithManifest):
    # The difference between cbench-v0 and cbench-v1 is the arguments passed to
    # clang when preparing the LLVM bitcodes:
    #
    #   - v0: `-O0 -Xclang -disable-O0-optnone`.
    #   - v1: `-O1 -Xclang -Xclang -disable-llvm-passes`.
    #
    # The key difference with is that in v0, the generated IR functions were
    # annotated with a `noinline` attribute that prevented inline. In v1 that is
    # no longer the case.
    def __init__(self, site_data_base: Path):
        platform = {"darwin": "macos"}.get(sys.platform, sys.platform)
        url, sha256 = _CBENCH_LEGACY_TARS[platform]
        super().__init__(
            name="benchmark://cBench-v0",
            description="Runnable C benchmarks",
            license="BSD 3-Clause",
            references={
                "Paper": "https://arxiv.org/pdf/1407.3487.pdf",
                "Homepage": "https://ctuning.org/wiki/index.php/CTools:CBench",
            },
            tar_urls=[url],
            tar_sha256=sha256,
            manifest_urls=[
                "https://dl.fbaipublicfiles.com/compiler_gym/llvm_bitcodes-10.0.0-cBench-v0-manifest.bz2"
            ],
            manifest_sha256="635b94eeb2784dfedb3b53fd8f84517c3b4b95d851ddb662d4c1058c72dc81e0",
            strip_prefix="cBench-v0",
            benchmark_file_suffix=".bc",
            site_data_base=site_data_base,
            deprecated="Please use 'benchmark://cbench-v1'",
        )


# ===============================
# Definition of cBench validators
# ===============================


# A map from benchmark name to validation callbacks.
VALIDATORS: Dict[
    str, List[Callable[["LlvmEnv"], Optional[str]]]  # noqa: F821
] = defaultdict(list)


# A map from benchmark name to BenchmarkDynamicConfig messages.
DYNAMIC_CONFIGS: Dict[str, Optional[BenchmarkDynamicConfig]] = {}


def validate_sha_output(result: BenchmarkExecutionResult) -> Optional[str]:
    """SHA benchmark prints 5 random hex strings. Normally these hex strings are
    16 characters but occasionally they are less (presumably because of a
    leading zero being omitted).
    """
    try:
        if not re.match(
            r"[0-9a-f]{0,16} [0-9a-f]{0,16} [0-9a-f]{0,16} [0-9a-f]{0,16} [0-9a-f]{0,16}",
            result.output.rstrip(),
        ):
            return "Failed to parse hex output"
    except UnicodeDecodeError:
        return "Failed to parse unicode output"


def setup_ghostscript_library_files(dataset_id: int) -> Callable[[Path], None]:
    """Make a pre-execution setup hook for ghostscript."""

    def setup(cwd: Path):
        cbench_data = site_data_path("llvm-v0/cbench-v1-runtime-data/runtime_data")
        # Copy the input data file into the current directory since ghostscript
        # doesn't like long input paths.
        shutil.copyfile(
            cbench_data / "office_data" / f"{dataset_id}.ps", cwd / "input.ps"
        )
        # Ghostscript doesn't like the library files being symlinks so copy them
        # into the working directory as regular files.
        for path in (cbench_data / "ghostscript").iterdir():
            if path.name.endswith(".ps"):
                shutil.copyfile(path, cwd / path.name)

    return setup


validator(
    benchmark="benchmark://cbench-v1/bitcount",
    cmd="$BIN 1125000",
)

validator(
    benchmark="benchmark://cbench-v1/bitcount",
    cmd="$BIN 512",
)

for i in range(1, 21):

    # NOTE(cummins): Disabled due to timeout errors, further investigation
    # needed.
    #
    # validator(
    #     benchmark="benchmark://cbench-v1/adpcm",
    #     cmd=f"$BIN $D/telecom_data/{i}.adpcm",
    #     data=[f"telecom_data/{i}.adpcm"],
    # )
    #
    # validator(
    #     benchmark="benchmark://cbench-v1/adpcm",
    #     cmd=f"$BIN $D/telecom_data/{i}.pcm",
    #     data=[f"telecom_data/{i}.pcm"],
    # )

    validator(
        benchmark="benchmark://cbench-v1/blowfish",
        cmd=f"$BIN d $D/office_data/{i}.benc output.txt 1234567890abcdeffedcba0987654321",
        data=[f"office_data/{i}.benc"],
        outs=["output.txt"],
    )

    validator(
        benchmark="benchmark://cbench-v1/bzip2",
        cmd=f"$BIN -d -k -f -c $D/bzip2_data/{i}.bz2",
        data=[f"bzip2_data/{i}.bz2"],
    )

    validator(
        benchmark="benchmark://cbench-v1/crc32",
        cmd=f"$BIN $D/telecom_data/{i}.pcm",
        data=[f"telecom_data/{i}.pcm"],
    )

    validator(
        benchmark="benchmark://cbench-v1/dijkstra",
        cmd=f"$BIN $D/network_dijkstra_data/{i}.dat",
        data=[f"network_dijkstra_data/{i}.dat"],
    )

    validator(
        benchmark="benchmark://cbench-v1/gsm",
        cmd=f"$BIN -fps -c $D/telecom_gsm_data/{i}.au",
        data=[f"telecom_gsm_data/{i}.au"],
    )

    # NOTE(cummins): ispell fails with returncode 1 and no output when run
    # under safe optimizations.
    #
    # validator(
    #     benchmark="benchmark://cbench-v1/ispell",
    #     cmd=f"$BIN -a -d americanmed+ $D/office_data/{i}.txt",
    #     data = [f"office_data/{i}.txt"],
    # )

    validator(
        benchmark="benchmark://cbench-v1/jpeg-c",
        cmd=f"$BIN -dct int -progressive -outfile output.jpeg $D/consumer_jpeg_data/{i}.ppm",
        data=[f"consumer_jpeg_data/{i}.ppm"],
        outs=["output.jpeg"],
        # NOTE(cummins): AddressSanitizer disabled because of
        # global-buffer-overflow in regular build.
        sanitizers=[LlvmSanitizer.TSAN, LlvmSanitizer.UBSAN],
    )

    validator(
        benchmark="benchmark://cbench-v1/jpeg-d",
        cmd=f"$BIN -dct int -outfile output.ppm $D/consumer_jpeg_data/{i}.jpg",
        data=[f"consumer_jpeg_data/{i}.jpg"],
        outs=["output.ppm"],
    )

    validator(
        benchmark="benchmark://cbench-v1/patricia",
        cmd=f"$BIN $D/network_patricia_data/{i}.udp",
        data=[f"network_patricia_data/{i}.udp"],
        env={
            # NOTE(cummins): Benchmark leaks when executed with safe optimizations.
            "ASAN_OPTIONS": "detect_leaks=0",
        },
    )

    validator(
        benchmark="benchmark://cbench-v1/qsort",
        cmd=f"$BIN $D/automotive_qsort_data/{i}.dat",
        data=[f"automotive_qsort_data/{i}.dat"],
        outs=["sorted_output.dat"],
        linkopts=["-lm"],
    )

    # NOTE(cummins): Rijndael benchmark disabled due to memory errors under
    # basic optimizations.
    #
    # validator(benchmark="benchmark://cbench-v1/rijndael", cmd=f"$BIN
    #     $D/office_data/{i}.enc output.dec d
    #     1234567890abcdeffedcba09876543211234567890abcdeffedcba0987654321",
    #     data=[f"office_data/{i}.enc"], outs=["output.dec"],
    # )
    #
    # validator(benchmark="benchmark://cbench-v1/rijndael", cmd=f"$BIN
    #     $D/office_data/{i}.txt output.enc e
    #     1234567890abcdeffedcba09876543211234567890abcdeffedcba0987654321",
    #     data=[f"office_data/{i}.txt"], outs=["output.enc"],
    # )

    validator(
        benchmark="benchmark://cbench-v1/sha",
        cmd=f"$BIN $D/office_data/{i}.txt",
        data=[f"office_data/{i}.txt"],
        compare_output=False,
        validate_result=validate_sha_output,
    )

    validator(
        benchmark="benchmark://cbench-v1/stringsearch",
        cmd=f"$BIN $D/office_data/{i}.txt $D/office_data/{i}.s.txt output.txt",
        data=[f"office_data/{i}.txt"],
        outs=["output.txt"],
        env={
            # NOTE(cummins): Benchmark leaks when executed with safe optimizations.
            "ASAN_OPTIONS": "detect_leaks=0",
        },
        linkopts=["-lm"],
    )

    # NOTE(cummins): The stringsearch2 benchmark has a very long execution time.
    # Use only a single input to keep the validation time reasonable. I have
    # also observed Segmentation fault on gold standard using 4.txt and 6.txt.
    if i == 1:
        validator(
            benchmark="benchmark://cbench-v1/stringsearch2",
            cmd=f"$BIN $D/office_data/{i}.txt $D/office_data/{i}.s.txt output.txt",
            data=[f"office_data/{i}.txt"],
            outs=["output.txt"],
            env={
                # NOTE(cummins): Benchmark leaks when executed with safe optimizations.
                "ASAN_OPTIONS": "detect_leaks=0",
            },
            # TSAN disabled because of extremely long execution leading to
            # timeouts.
            sanitizers=[LlvmSanitizer.ASAN, LlvmSanitizer.MSAN, LlvmSanitizer.UBSAN],
        )

    validator(
        benchmark="benchmark://cbench-v1/susan",
        cmd=f"$BIN $D/automotive_susan_data/{i}.pgm output_large.corners.pgm -c",
        data=[f"automotive_susan_data/{i}.pgm"],
        outs=["output_large.corners.pgm"],
        linkopts=["-lm"],
    )

    validator(
        benchmark="benchmark://cbench-v1/tiff2bw",
        cmd=f"$BIN $D/consumer_tiff_data/{i}.tif output.tif",
        data=[f"consumer_tiff_data/{i}.tif"],
        outs=["output.tif"],
        linkopts=["-lm"],
        env={
            # NOTE(cummins): Benchmark leaks when executed with safe optimizations.
            "ASAN_OPTIONS": "detect_leaks=0",
        },
    )

    validator(
        benchmark="benchmark://cbench-v1/tiff2rgba",
        cmd=f"$BIN $D/consumer_tiff_data/{i}.tif output.tif",
        data=[f"consumer_tiff_data/{i}.tif"],
        outs=["output.tif"],
        linkopts=["-lm"],
    )

    validator(
        benchmark="benchmark://cbench-v1/tiffdither",
        cmd=f"$BIN $D/consumer_tiff_data/{i}.bw.tif out.tif",
        data=[f"consumer_tiff_data/{i}.bw.tif"],
        outs=["out.tif"],
        linkopts=["-lm"],
    )

    validator(
        benchmark="benchmark://cbench-v1/tiffmedian",
        cmd=f"$BIN $D/consumer_tiff_data/{i}.nocomp.tif output.tif",
        data=[f"consumer_tiff_data/{i}.nocomp.tif"],
        outs=["output.tif"],
        linkopts=["-lm"],
    )

    # NOTE(cummins): On macOS the following benchmarks abort with an illegal
    # hardware instruction error.
    # if sys.platform != "darwin":
    #     validator(
    #         benchmark="benchmark://cbench-v1/lame",
    #         cmd=f"$BIN $D/consumer_data/{i}.wav output.mp3",
    #         data=[f"consumer_data/{i}.wav"],
    #         outs=["output.mp3"],
    #         compare_output=False,
    #         linkopts=["-lm"],
    #     )

    # NOTE(cummins): Segfault on gold standard.
    #
    #     validator(
    #         benchmark="benchmark://cbench-v1/ghostscript",
    #         cmd="$BIN -sDEVICE=ppm -dNOPAUSE -dQUIET -sOutputFile=output.ppm -- input.ps",
    #         data=[f"office_data/{i}.ps"],
    #         outs=["output.ppm"],
    #         linkopts=["-lm", "-lz"],
    #         pre_execution_callback=setup_ghostscript_library_files(i),
    #     )
