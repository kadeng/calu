package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.BridgeView
import io.github.kadeng.calu.state.SetupHint
import io.github.kadeng.calu.state.initialViewState
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** The card for a machine without calu. */
class MissingCardTest {
    @Test
    fun `calu missing - the card offers the installation first and the hint's button second`() {
        val bare = initialViewState().copy(bridge = BridgeView(BridgeStatus.MISSING, "calu was not found."))
        assertEquals(CardAction("Install calu", "install"), setupCards(bare).single().primary)
        assertEquals(emptyList<CardAction>(), setupCards(bare).single().secondary)
        val hinted = bare.copy(hints = listOf(SetupHint("calu-missing", "error", "calu is not installed on this machine.", "openSettings", "Open settings")))
        val card = setupCards(hinted).single()
        assertEquals("calu is not installed on this machine.", card.message)
        assertEquals("install", card.primary!!.action)
        assertEquals(listOf(CardAction("Open settings", "openSettings")), card.secondary)
    }
}
