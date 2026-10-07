/* The page shown for a top-nav page whose module is not built yet. */

import { h } from "../ui.js";

export default {
  render(view) {
    view.append(h("div", { class: "subbar" }), h("p", { class: "empty", text: "还没接上" }));
  },
};
