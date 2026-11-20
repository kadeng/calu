/**
 * Data the backend attaches to a diff it opens, for the comment action of the frontend.
 *
 *     EditorActions.diffRequest ── request.putUserData(CALU_DIFF_REF, "edit:<tool_use_id>")
 *     CommentAction             ── request.getUserData(CALU_DIFF_REF) ── comment.add {ref}
 *
 * User data reaches the frontend only when both halves run in one process (a local IDE, also
 * in the SSH mode); in remote development the client's diff carries no request user data, and
 * the frontend asks the backend (`CaluRemoteApi.diffOfText`), which returns the same `ref`.
 */
package io.github.kadeng.calu.remote

import com.intellij.openapi.util.Key

/** The `ref` of a comment made in a diff of one of Claude's edits: "edit:<tool_use_id>" (`editRef`). */
val CALU_DIFF_REF: Key<String> = Key.create("calu.diff.ref")
