package com.example.bg_color;

import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.view.View;
import android.graphics.Color;
import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        LinearLayout ll = findViewById(R.id.ll);
        Button b1 = findViewById(R.id.red);
        Button b2 = findViewById(R.id.green);
        Button b3 = findViewById(R.id.blue);

        b1.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                ll.setBackgroundColor(Color.RED);
            }
        });
        b2.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                ll.setBackgroundColor(Color.GREEN);
            }
        });
        b3.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                ll.setBackgroundColor(Color.BLUE);
            }
        });


    }
}