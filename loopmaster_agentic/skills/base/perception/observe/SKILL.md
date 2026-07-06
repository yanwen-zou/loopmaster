---
name: observe
category: base/perception
description: Read the latest HEI ReBot Lift observation, including proprioception and camera metadata.
args:
  include_images: bool
  include_state: bool
---

# Observe

Use this as the first step in a real-robot run. It calls the platform's
`observe()` method and stores the observation in the skill context for later
skills.
