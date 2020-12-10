package MKAgent;

import java.util.List;
import java.util.Random;

public class RandomAgent implements Agent {

    private static final Random random = new Random();

    @Override
    public int getMove(Board board, Side side) {
        List<Integer> validMoves = board.getValidMoves(side);
        return validMoves.get(random.nextInt(validMoves.size()));
    }
}
