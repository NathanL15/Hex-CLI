import onnxruntime as ort


session = ort.InferenceSession(
    "model.onnx",
    providers=[
        (
            "QNNExecutionProvider",
            {
                "backend_type": "htp",
                "htp_performance_mode": "balanced",
                "qnn_context_priority": "normal_high",
                "profiling_level": "off",
            },
        ),
        "CPUExecutionProvider",
    ],
)

print(session.get_providers())
