# Vision

The Phase 9 vision subsystem defines OCR, screen understanding, window detection, image understanding, and document understanding boundaries.

## Components

- `OcrResult`: extracted text, confidence, and regions
- `DetectedWindow`: window title, bounds, and confidence
- `ScreenUnderstanding`: screen summary, windows, and OCR text
- `ImageObservation`: image summary, labels, and metadata
- `DocumentUnderstanding`: title, summary, sections, and metadata
- `VisionProcessor`: adapter interface for OCR and visual understanding engines
- `BasicScreenUnderstandingProcessor`: dependency-free baseline processor
- `VisionModule`: event-driven runtime module

## Events

- `vision.task_requested`: request OCR, screen understanding, window detection, image understanding, or document understanding
- `vision.task_completed`: emitted with structured vision results

## Future Adapters

OpenCV, EasyOCR, local multimodal models, cloud vision models, and screen/window APIs can implement the `VisionProcessor` interface without changing the core runtime.
