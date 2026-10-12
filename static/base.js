/* Where this page lives. A second home's pages sit under /home/<name>, and every
   address the page asks for has to carry that prefix too. The server writes it
   on <html data-base>; for the first home it is empty and nothing changes. */

export const BASE = document.documentElement.dataset.base || "";
export const url = (path) => BASE + path;
