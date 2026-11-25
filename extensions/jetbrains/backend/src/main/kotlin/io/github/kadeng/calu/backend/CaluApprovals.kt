package io.github.kadeng.calu.backend

import com.intellij.openapi.components.BaseState
import com.intellij.openapi.components.RoamingType
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.SimplePersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import io.github.kadeng.calu.logic.approvalDigest

/** Approvals kept at most; the oldest is dropped first. */
const val MAX_APPROVALS: Int = 500

/** The stored form of `CaluApprovals`. */
class CaluApprovalsState : BaseState() {
    /** SHA-256 digests (`approvalDigest`) of approved `launchContent` texts, oldest first. */
    var digests by list<String>()
}

/**
 * The project settings a person approved in this IDE (D279, S4): digests of `launchContent`,
 * stored in the IDE's own configuration (`calu-approvals.xml`, not synchronised to other
 * machines), never in a project. One approval serves every project with the same settings.
 */
@Service(Service.Level.APP)
@State(name = "CaluApprovals", storages = [Storage(value = "calu-approvals.xml", roamingType = RoamingType.DISABLED)])
class CaluApprovals : SimplePersistentStateComponent<CaluApprovalsState>(CaluApprovalsState()) {
    /**
     * Tell whether these settings were approved.
     *
     * @param content The text of `launchContent`.
     * @return True if its digest is stored.
     */
    fun isApproved(content: String): Boolean = synchronized(this) { approvalDigest(content) in state.digests }

    /**
     * Store the approval of these settings. Side effect: the IDE writes the file at its next save.
     *
     * @param content The text of `launchContent`.
     */
    fun approve(content: String) = synchronized(this) {
        val digest = approvalDigest(content)
        if (digest in state.digests) return@synchronized
        state.digests.add(digest)
        while (state.digests.size > MAX_APPROVALS) state.digests.removeAt(0)
        state.intIncrementModificationCount()
    }

    companion object {
        /**
         * Return the application's approvals.
         *
         * @return The service.
         */
        fun get(): CaluApprovals = service()
    }
}
