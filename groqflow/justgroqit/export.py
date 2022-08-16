import os
import inspect
import shutil
import re
import torch
import onnxruntime
import onnxmltools
import onnx
import groqflow.justgroqit.stage as stage
import groqflow.common.exceptions as exp
import groqflow.common.build as build
import groqflow.common.tensor_helpers as tensor_helpers
import groqflow.common.onnx_helpers as onnx_helpers
import groqflow.common.sdk_helpers as sdk


def _check_model(onnx_file, success_message, fail_message) -> bool:
    if os.path.isfile(onnx_file):
        print(success_message)
    else:
        print(fail_message)
        return False
    try:
        onnx.checker.check_model(onnx_file)
        print("\tSuccessfully checked onnx file")
        return True
    except onnx.checker.ValidationError as e:
        print("\tError while checking generated ONNX file")
        print(e)
        return False


class ReceiveOnnxModel(stage.GroqitStage):
    """
    Stage that takes an ONNX model as input.

    Expected inputs:
     - state.model is a path to the ONNX model
     - state.inputs is a dict that represents valid inputs for the onnx model

    Outputs:
     - A *-base.onnx file that implements state.model given state.inputs.
    """

    def __init__(self):
        super().__init__(
            unique_name="receive_onnx",
            monitor_message="Receiving ONNX Model",
        )

    def fire(self, state: build.State):
        if not isinstance(state.model, str):
            msg = f"""
            The current stage (ReceiveOnnxModel) is only compatible with
            ONNX files, however the stage received a model of type
            {type(state.model)}.
            """
            raise exp.GroqitStageError(msg)
        if not state.model.endswith(".onnx"):
            msg = f"""
            The current stage (ReceiveOnnxModel) expects a path to ONNX
            model, however the stage received {state.model}.
            """
            raise exp.GroqitStageError(msg)

        dummy_inputs = tuple(state.inputs.values())
        dummy_input_names = tuple(state.inputs.keys())
        state.inputs = dict(zip(dummy_input_names, dummy_inputs))

        model = onnx.load(state.model)
        opset_str = str(model.opset_import)  # pylint: disable=no-member
        opset = int(re.search(r"\d+", opset_str).group())
        input_shapes = [
            [d.dim_value for d in _input.type.tensor_type.shape.dim]
            for _input in model.graph.input  # pylint: disable=no-member
        ]

        # Check for Dynamic shapes in the model. They can be represented as 0, -1, "unk__".
        for input in input_shapes:
            for dimension in input:
                if dimension < 1 or not isinstance(dimension, int):
                    msg = f"""
                    The received model has dynamic input dimensions. Please freeze the model with static
                    input dimensions.
                    More information may be available in the log file at **{self.logfile_path}**
                    """
                    raise exp.GroqitStageError(msg)

        if opset < build.DEFAULT_ONNX_OPSET and opset >= build.MINIMUM_ONNX_OPSET:
            print(
                " \n The received model has an opset {opset}. Though this opset is supported \
                we recommend upgrading the model to opset {build.MINIMUM_ONNX_OPSET}"
            )
        elif opset < build.MINIMUM_ONNX_OPSET:
            msg = f"""
            The received model has an opset {opset}. Opset < 11 is not supported. Please
            try upgrading the model to opset 13.
            More information may be available in the log file at **{self.logfile_path}**
            """
            raise exp.GroqitStageError(msg)

        shutil.move(state.model, state.base_onnx_file)

        # Save inputs and convert to fp16 and int32 (no int64 nor float32/64)
        tensor_helpers.save_inputs([state.inputs], state.original_inputs_file)

        # Check the if the base mode has been exported successfully
        success_msg = "\tSuccess receiving ONNX Model"
        fail_msg = "\tFailed receiving ONNX Model"
        state.info.base_onnx_exported = _check_model(
            state.base_onnx_file, success_msg, fail_msg
        )

        if state.info.base_onnx_exported:
            state.intermediate_results = [state.base_onnx_file]
        else:
            msg = f"""
            Unable to process ONNX Model. We recommend that you verify the source of the model.
            Any optimizations performed on the model could result in an error.
            More information may be available in the log file at **{self.logfile_path}**
            """
            raise exp.GroqitStageError(msg)

        return state


class ExportPytorchModel(stage.GroqitStage):
    """
    Stage that takes a PyTorch model instance, in state.model, and
    exports it to an ONNX file.

    Expected inputs:
     - state.model is a torch.nn.Module or torch.jit.ScriptModule
     - state.inputs is a dict that represents valid kwargs to the forward
        function of state.model

    Outputs:
     - A *-base.onnx file that implements state.model given state.inputs
    """

    def __init__(self):
        super().__init__(
            unique_name="export_pytorch",
            monitor_message="Exporting PyTorch to ONNX",
        )

    def fire(self, state: build.State):
        if not isinstance(state.model, (torch.nn.Module, torch.jit.ScriptModule)):
            msg = f"""
            The current stage (ExportPytorchModel) is only compatible with
            models of type torch.nn.Module or torch.jit.ScriptModule, however
            the stage received a model of type {type(state.model)}.
            """
            raise exp.GroqitStageError(msg)

        # TODO: check that state.inputs is valid
        # https://git.groq.io/code/Groq/-/issues/13947

        # ONNX export only accepts positional inputs
        if isinstance(state.model, torch.jit.ScriptModule):
            dummy_inputs = tuple(state.inputs.values())
            dummy_input_names = tuple(state.inputs.keys())
        else:
            all_args = list(inspect.signature(state.model.forward).parameters.keys())
            user_provided_args = state.inputs.keys()
            inputs = []
            input_names = []

            for inp in user_provided_args:
                if inp not in all_args:
                    msg = f"""
                    Input name {inp} not found in the model's forward method. Available
                    inputs are: {all_args}"
                    """
                    raise ValueError(msg)

            for _, arg in enumerate(all_args):
                if arg in user_provided_args:
                    inputs.append(state.inputs[arg])
                else:
                    inputs.append(None)
                input_names.append(arg)
            dummy_inputs = tuple(inputs)
            dummy_input_names = tuple(input_names)

        state.info.opset = build.DEFAULT_ONNX_OPSET

        # Export the model to ONNX
        # Note: use_external_data_format causes a bug when converting to fp16
        torch.onnx.export(
            state.model,
            dummy_inputs,
            state.base_onnx_file,
            input_names=dummy_input_names,
            output_names=["output"],
            do_constant_folding=True,
            opset_version=state.info.opset,
            verbose=False,
        )
        state.inputs = dict(zip(dummy_input_names, dummy_inputs))

        # Save inputs and convert to fp16 and int32 (no int64 nor float32/64)
        tensor_helpers.save_inputs([state.inputs], state.original_inputs_file)

        # Check the if the base mode has been exported successfully
        success_msg = "\tSuccess exporting model to ONNX"
        fail_msg = "\tFailed exporting model to ONNX"
        state.info.base_onnx_exported = _check_model(
            state.base_onnx_file, success_msg, fail_msg
        )

        if state.info.base_onnx_exported:
            state.intermediate_results = [state.base_onnx_file]
        else:
            msg = f"""
            Unable to export model to ONNX using Torch's ONNX exporter.
            We recommend that you modify your model until it is
            compatible with this third party software, then re-run groqit().
            More information may be available in the log file at **{self.logfile_path}**
            """
            raise exp.GroqitStageError(msg)

        return state


class OptimizeOnnxModel(stage.GroqitStage):
    """
    Stage that takes an ONNX file and uses ONNX Runtime to optimize it.
    Important because this helps to perform constant folding, Redundant
    node eliminations, Semantics-preserving node fusions

    Expected inputs:
     - state.intermediate_results contains a single .onnx file

    Outputs:
     - A *-opt.onnx file
    """

    def __init__(self):
        super().__init__(
            unique_name="optimize_onnx",
            monitor_message="Optimizing ONNX file",
        )

    def fire(self, state: build.State):

        # TODO: validate this input
        # https://git.groq.io/code/Groq/-/issues/13947
        input_onnx = state.intermediate_results[0]

        # Perform some basic optimizations on the model to remove shape related
        # information inserted for dynamic shape inference.
        # Given that we're compiling against a fixed sequence length the dynamic
        # shape information is not necessary
        session_options = onnxruntime.SessionOptions()

        # Set graph optimization level
        session_options.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )

        # To enable model serialization after graph optimization set this
        session_options.optimized_model_filepath = state.opt_onnx_file

        # Optimize graph
        onnxruntime.InferenceSession(input_onnx, session_options)

        # Check that the converted model is still valid
        success_msg = "\tSuccess optimizing ONNX model"
        fail_msg = "\tFailed optimizing ONNX model"
        state.info.opt_onnx_exported = _check_model(
            state.opt_onnx_file, success_msg, fail_msg
        )

        if state.info.opt_onnx_exported:
            state.intermediate_results = [state.opt_onnx_file]
        else:
            msg = f"""
            Unable to optimize ONNX file using ONNX runtime.
            We recommend that you modify your model until it is
            compatible with this third party software, then re-run groqit().
            More information may be available in the log file at **{self.logfile_path}**
            """
            raise exp.GroqitStageError(msg)

        return state


class CheckOnnxCompatibility(stage.GroqitStage):
    """
    Stage that takes an ONNX file, checks whether it is compatible
    with Groq Compiler, and raises an exception if the ONNX file is
    not compatible.

    Expected inputs:
     - state.intermediate_results contains a single .onnx file

    Outputs:
     - The same ONNX file as the input
    """

    def __init__(self):
        super().__init__(
            unique_name="check_compatibility",
            monitor_message="Checking for Op support",
        )

    def fire(self, state: build.State):

        sdk.check_dependencies(
            require_devtools=True, exception_type=exp.GroqitStageError
        )

        # TODO: validate this input
        # https://git.groq.io/code/Groq/-/issues/13947
        input_onnx = state.intermediate_results[0]

        (
            state.info.opt_onnx_ops,
            state.info.opt_onnx_unsupported_ops,
        ) = onnx_helpers.check_ops(input_onnx, state.use_sdk)
        print(f"Model has {len(state.info.opt_onnx_unsupported_ops)} unsupported ops")

        state.info.opt_onnx_all_ops_supported = (
            len(state.info.opt_onnx_unsupported_ops) == 0
            and len(state.info.opt_onnx_ops) != 0
        )

        if not state.info.opt_onnx_all_ops_supported:
            ops = ", ".join(state.info.opt_onnx_unsupported_ops)
            msg = f"""
            You model contains ONNX operation(s) that are not supported by Groq Compiler:
            **{ops}**
            Please replace these operation(s) in your model or contact
            sales@groq.com to request improved operation support in Groq Compiler.
            """
            raise exp.GroqitStageError(msg)

        return state


class ConvertOnnxToFp16(stage.GroqitStage):
    """
    Stage that takes an ONNX file and converts its trained parameters
    to fp16.

    Expected inputs:
     - state.intermediate_results contains a single .onnx file

    Outputs:
     - A *-f16.onnx file with FP16 trained parameters
    """

    def __init__(self):
        super().__init__(
            unique_name="fp16_conversion",
            monitor_message="Converting to FP16",
        )

    def fire(self, state: build.State):

        # TODO: validate this input
        # https://git.groq.io/code/Groq/-/issues/13947
        input_onnx = state.intermediate_results[0]

        # Convert the model to FP16
        fp32_model = onnx.load_model(input_onnx)
        fp16_model = onnxmltools.utils.float16_converter.convert_float_to_float16(
            fp32_model,
        )

        # Save F16 model
        output_path = state.converted_onnx_file
        onnxmltools.utils.save_model(fp16_model, output_path)

        # Check that the converted model is still valid
        success_msg = "\tSuccess converting ONNX model to fp16"
        fail_msg = "\tFailed converting ONNX model to fp16"
        state.info.converted_onnx_exported = _check_model(
            state.converted_onnx_file, success_msg, fail_msg
        )

        if state.info.converted_onnx_exported:
            state.intermediate_results = [output_path]
        else:
            msg = f"""
            Attempted to use onnxmltools, a third party library, to convert your
            model to the float16 datatype, however this operation was not successful.
            More information may be available in the log file at **{self.logfile_path}**
            """
            raise exp.GroqitStageError(msg)

        return state