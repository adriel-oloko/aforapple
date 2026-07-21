# Lucy 2.5

> Real-time, prompt-driven video editing over WebRTC. Restyle, swap backgrounds, and add or replace objects live on a webcam or streamed feed at interactive latency.


## Overview

- **Endpoint**: `https://fal.run/decart/lucy-2-5/realtime`
- **Model ID**: `decart/lucy-2-5/realtime`
- **Category**: video-to-video
- **Kind**: inference
**Description**: Real-time video-to-video editing over WebRTC. Swap backgrounds, restyle, add/remove/replace objects, apply VFX, do character-swap and virtual try-on live on a webcam or streamed feed, all at interactive latency.

**Tags**: realtime, video-to-video, webrtc



## Pricing

- **Price**: $0.04 per seconds

For more details, see [fal.ai pricing](https://fal.ai/pricing).

## API Information

This model can be used via our HTTP API or more conveniently via our client libraries.
See the input and output schema below, as well as the usage examples.


### Input Schema

The API accepts the following input parameters:


- **`reference_image_url`** (`string`, _optional_):
  Reference image (data URI, JPEG/PNG/WebP, min 512x512) for character swap or virtual try-on. Optional for other capabilities. Default value: `"null"`
  - Default: `null`

- **`prompt`** (`string`, _optional_):
  Edit instruction. Drives every capability: add / replace / remove / background swap / style / VFX / change-attribute (prompt only), and character-swap / virtual-try-on (together with a reference image). Default value: `"null"`
  - Default: `null`
  - Examples: "Change the background to a sunlit tropical beach."

- **`enable_prompt_expansion`** (`boolean`, _optional_):
  Whether Decart should expand/enhance the prompt before applying it. Default value: `true`
  - Default: `true`

- **`image_url`** (`string`, _optional_):
  Live camera frame source (enables the webcam widget; unused in WebRTC mode). Default value: `"null"`
  - Default: `null`



**Required Parameters Example**:

```json
{}
```

**Full Example**:

```json
{
  "prompt": "Change the background to a sunlit tropical beach.",
  "enable_prompt_expansion": true
}
```


### Output Schema

The API returns the following output format:

- **`iceServers`** (`list<object>`, _optional_)
  - Default: `null`
  - Array of object

- **`type`** (`string`, _optional_):
   Default value: `"null"`
  - Default: `null`

- **`sdp`** (`string`, _optional_):
   Default value: `"null"`
  - Default: `null`

- **`error`** (`void`, _optional_)
  - Default: `null`

- **`candidate`** (`object`, _optional_)
  - Default: `null`



**Example Response**:

```json
{}
```


## Usage Examples

### cURL

```bash
curl --request WEBSOCKET \
  --url https://fal.run/decart/lucy-2-5/realtime \
  --header "Authorization: Key $FAL_KEY" \
  --header "Content-Type: application/json" \
  --data '{}'
```

### Python

Ensure you have the Python client installed:

```bash
pip install fal-client
```

Then use the API client to make requests:

```python
import fal_client

def on_queue_update(update):
    if isinstance(update, fal_client.InProgress):
        for log in update.logs:
           print(log["message"])

result = fal_client.subscribe(
    "decart/lucy-2-5/realtime",
    arguments={},
    with_logs=True,
    on_queue_update=on_queue_update,
)
print(result)
```

### JavaScript

Ensure you have the JavaScript client installed:

```bash
npm install --save @fal-ai/client
```

Then use the API client to make requests:

```javascript
import { fal } from "@fal-ai/client";

const result = await fal.subscribe("decart/lucy-2-5/realtime", {
  input: {},
  logs: true,
  onQueueUpdate: (update) => {
    if (update.status === "IN_PROGRESS") {
      update.logs.map((log) => log.message).forEach(console.log);
    }
  },
});
console.log(result.data);
console.log(result.requestId);
```


## Additional Resources

### Documentation

- [Model Playground](https://fal.ai/models/decart/lucy-2-5/realtime)
- [API Documentation](https://fal.ai/models/decart/lucy-2-5/realtime/api)
- [OpenAPI Schema](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=decart/lucy-2-5/realtime)

### fal.ai Platform

- [Platform Documentation](https://docs.fal.ai)
- [Python Client](https://docs.fal.ai/clients/python)
- [JavaScript Client](https://docs.fal.ai/clients/javascript)
